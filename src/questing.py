import asyncio

from typing import Optional
from enum import Enum, auto

from loguru import logger

import regex

from src.command_parser import parse_command
from src.questbots import *
from src.teleport_math import *
from src.utils import *
from src.paths import *
from src.task_watcher import TaskWatcher
from src.barriers import *


class QuestKind(Enum):
    collect = auto()
    unknown = auto()


class TeleportResult(Enum):
    same_zone = auto()
    new_zone = auto()

class BotStepResult(Enum):
    step = auto()
    loop = auto()


class QuestCtx:
    ## The new leader mechanism. Multiple clients may share one quest context so they perform the same actions together.
    def __init__(self):
        self.owner: Optional[Client] = None
        self.full_name: Optional[str] = None
        self.clean_name: Optional[str] = None
        self.full_text: Optional[str] = None
        self.clean_text: Optional[str] = None
        self.quest_xyz: Optional[XYZ] = None


class BotCtx:
    def __init__(self, client: Client, bot: QuestBot, quester: "Quester"):
        self.client = client
        self.bot = bot
        self.quester = quester
        self.line_idx = 0
        self.code_lines = [x.strip() for x in self.bot.get_code().splitlines() if len(x.strip()) > 0]

    async def step(self) -> BotStepResult:
        result = BotStepResult.step
        instruction_line = self.code_lines[self.line_idx]
        self.line_idx += 1
        if self.line_idx >= len(self.code_lines):
            self.line_idx = 0
            result = BotStepResult.loop
        await parse_command([self.client], instruction_line, quest_tp_impl=self.quester._start_ctx_tp)
        return result

class Quester:
    def __init__(self, client: Client, bot_manager: QuestBotManager):
        self.client = client
        self.bot_manager = bot_manager
        self.current_bot: Optional[BotCtx] = None
        self.current_context: Optional[QuestCtx] = None
        self.prev_context: Optional[QuestCtx] = None
        self.barrier = TimedBarrier()
        self._teleport_result = SingleWriteValue[TeleportResult]()
        self._watchdog = TaskWatcher()
        self._teleport_task: Optional[asyncio.Task] = None

    async def _create_context(self):
        logger.debug("Refreshing quest context")
        ctx = QuestCtx()
        ctx.owner = self.client
        ctx.full_text = await self.fetch_quest_text()
        ctx.clean_text = self.clean_quest_text(ctx.full_text)
        ctx.full_name = await self.fetch_quest_name()
        ctx.clean_name = self.clean_quest_text(ctx.full_name)
        if self.prev_context is None or self.prev_context.clean_name != ctx.clean_name or self.prev_context.clean_text != ctx.clean_text:
            logger.debug(f'Quest: "{ctx.full_name}"')
            logger.debug(f'Goal: "{self.strip_quest_text(ctx.full_text)}"')
        ctx.quest_xyz = await self.client.quest_position.position()
        self.prev_context = self.current_context if self.current_context is not None else ctx
        self.current_context = ctx

    def _create_bot_context(self, bot: QuestBotInfo):
        logger.debug(f'Loading a bot for: "{bot.quest_name}" - "{bot.goal_name}"')
        ctx = BotCtx(self.client, bot.load_bot(), self)
        self.current_bot = ctx

    def identify_quest_kind(self, quest_text: str) -> QuestKind:
        ## Attempts to identify a quest kind, returns unknown if it is unable to do so.
        if quest_text.startswith("collect"):
            return QuestKind.collect
        logger.error(f"Unable to identify a special quest kind for quest: `{quest_text}`\nUsing fallback method.")
        return QuestKind.unknown

    def strip_quest_text(self, quest_text: str) -> str:
        clean_text = regex.sub(r"<\/?([^>])+>|\([^)]*\)", "", quest_text).strip()
        return clean_text

    def clean_quest_text(self, quest_text: str) -> str:
        ## Cleans quest text for use in other functions with uniform structure.
        # this regex strips out xml tags and text in parentehes
        return self.strip_quest_text(quest_text).lower()

    async def fetch_quest_name(self) -> str:
        character_registry = await self.client.character_registry()
        current_id = await character_registry.active_quest_id()
        quest_manager = await self.client.quest_manager()
        quests = await quest_manager.quest_data()
        for quest_id, quest_data in quests.items():
            if quest_id != current_id:
                continue
            lang_key = await quest_data.name_lang_key()
            if lang_key == "Quest Finder":
                return lang_key
            return await self.client.cache_handler.get_langcode_name(lang_key)
        assert False, "Unreachable"

    async def fetch_quest_text(self):
        ## Grabs the text instructions of a quest
        quest_text_window = await get_window_from_path(self.client.root_window, quest_name_path)
        while not quest_text_window:
            await asyncio.sleep(0.1)
            quest_text_window = await get_window_from_path(self.client.root_window, quest_name_path)
        quest_text = await quest_text_window.maybe_text()
        return quest_text

    async def _is_blocked(self):
        ## Inversion of is_free
        return not await is_free(self.client)

    def _start_ctx_tp(self, xyz: Optional[XYZ] = None):
        ## Uses the current context to perform a navmap tp
        async def _impl(self: Quester):
            assert self.current_context is not None
            assert self.current_context.quest_xyz is not None
            quest_xyz = xyz if xyz is not None else self.current_context.quest_xyz
            ticket = self.barrier.fetch()
            if self._teleport_result.filled():
                raise RuntimeError("Tried teleporting while previous teleport result hasn't been consumed.")
            skip_cooldown = False
            try:
                starting_zone_name = await self.client.zone_name()
                await navmap_tp(self.client, xyz=quest_xyz)
                if await self.client.is_loading() or await self.client.zone_name() != starting_zone_name:
                    self._teleport_result.write(TeleportResult.new_zone)
                else:
                    self._teleport_result.write(TeleportResult.same_zone)
                    skip_cooldown = True
            finally:
                self.barrier.submit(ticket, skip_cooldown=skip_cooldown)
        self._teleport_task = self._watchdog.new_task(_impl(self))

    async def _handle_interact(self):
        popup_window = await get_window_from_path(self.client.root_window, popup_title_path)
        if not popup_window or not await popup_window.is_visible():
            return
        await self.client.send_key(Keycode.X, 0.1)
        self.barrier.block_cooldown()

    def _goal_changed(self) -> bool:
        assert self.prev_context is not None and self.current_context is not None
        return self.prev_context.clean_name != self.current_context.clean_name or self.prev_context.clean_text != self.current_context.clean_text

    async def _step_impl(self, ctx: Optional[QuestCtx] = None):
        if await self._is_blocked():
            self.barrier.block_cooldown()
            await asyncio.sleep(0.5)
            return
        if self.barrier.is_blocked():
            return

        if ctx is not None:
            self.current_context = ctx
        elif self.current_context is not None:
            quest_text = self.clean_quest_text(await self.fetch_quest_text())
            quest_pos = await self.client.quest_position.position()
            assert self.current_context.quest_xyz is not None
            if quest_text != self.current_context.clean_text or calc_Distance(quest_pos, self.current_context.quest_xyz) > 1.0:
                # New quest text, update the context
                await self._create_context()
        else:
            # There is no context and we didn't receive one. Make a new one
            await self._create_context()

        if self.current_bot is not None:
            if self._teleport_result.filled():
                self._teleport_result.consume()
                assert self._teleport_task is not None
                self._watchdog.unregister(self._teleport_task)
                self._teleport_task = None
            if self._goal_changed():
                # the goal changed, so we must unload the bot
                self.current_bot = None
            else:
                if await self.current_bot.step() == BotStepResult.loop:
                    self.barrier.block_cooldown()
                return

        assert self.current_context is not None and self.prev_context is not None
        assert self.current_context.clean_name is not None
        assert self.current_context.clean_text is not None
        if self.bot_manager.has_bot_for(self.current_context.clean_name, self.current_context.clean_text):
            if self.current_bot is None:
                bot_info = self.bot_manager.fetch_bot_info(self.current_context.clean_name, self.current_context.clean_text)
                self._create_bot_context(bot_info)
                self.barrier.block_cooldown()
                return

        if self._teleport_result.filled():
            # a teleport has happened and it has information for us
            tp_result = self._teleport_result.consume()
            assert self._teleport_task is not None
            self._watchdog.unregister(self._teleport_task)
            self._teleport_task = None
            if tp_result == TeleportResult.same_zone:
                logger.debug("Zone did not change.")
                try:
                    await asyncio.wait_for(wait_for_visible_by_path(self.client, npc_range_path), timeout=5.0)
                except TimeoutError:
                    # either gonna be combat or a very slow zone transfer, do nothing.
                    pass
                else:
                    # it did not time out, there should be an interactible here.
                    logger.debug("Dialog found after tp.")
                    await self._handle_interact()
            else:
                # Zone transfer. Empty branch here for documentation purposes.
                logger.debug("Detected a zone change.")
        else:
            # begin a teleport, forces a block and waits for ticket
            self._start_ctx_tp()


    async def step(self, ctx: Optional[QuestCtx] = None):
        try:
            await self._step_impl(ctx)
        except Exception as e:
            logger.exception("Questing error", e)
            raise
