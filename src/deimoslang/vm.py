import asyncio

from wizwalker import Client, XYZ

from .tokenizer import *
from .parser import *
from .ir import *

from wizwalker.extensions.wizsprinter import SprintyClient
from wizwalker.extensions.wizsprinter.wiz_sprinter import upgrade_clients
from src.utils import is_visible_by_path, is_free, get_window_from_path
from src.command_parser import teleport_to_friend_from_list

from loguru import logger


class VMError(Exception):
    pass


class VM:
    def __init__(self, clients: list[Client]):
        self._clients = upgrade_clients(clients) # guarantee it's usable
        self.program: list[Instruction] = []
        self.running = False
        self.killed = False
        self._ip = 0 # instruction pointer
        self._callstack = []

    def reset(self):
        self.program = []
        self._ip = 0
        self._callstack = []

    def stop(self):
        self.running = False

    def kill(self):
        self.stop()
        self.killed = True

    def load_from_text(self, code: str):
        compiler = Compiler.from_text(code)
        self.program = compiler.compile()

    def player_by_num(self, num: int) -> Client:
        i = num - 1
        if i >= len(self._clients):
            tail = "client is open" if len(self._clients) == 1 else "clients are open"
            raise VMError(f"Attempted to get client {num}, but only {len(self._clients)} {tail}")
        return self._clients[i]

    def _select_players(self, selector: PlayerSelector) -> list[SprintyClient]:
        if selector.mass:
            return self._clients
        else:
            result: list[SprintyClient] = []
            if selector.inverted:
                for i, c in enumerate(self._clients):
                    if i in selector.player_nums:
                        continue
                    result.append(c)
            else:
                for i, c in enumerate(self._clients):
                    if i in selector.player_nums:
                        result.append(c)
            return result

    async def _eval_command_expression(self, expression: CommandExpression):
        assert expression.command.kind == CommandKind.expr
        assert type(expression.command.data) is list
        assert type(expression.command.data[0]) is ExprKind

        selector = expression.command.player_selector
        assert selector is not None
        clients = self._select_players(selector)
        match expression.command.data[0]:
            case ExprKind.window_visible:
                for client in clients:
                    if not await is_visible_by_path(client, expression.command.data[1]):
                        return False
                return True
            case ExprKind.in_zone:
                for client in clients:
                    zone = await client.zone_name()
                    expected = "/".join(expression.command.data[1])
                    if expected != zone:
                        return False
                return True
            case ExprKind.same_zone:
                a = self.player_by_num(expression.command.data[1].value)
                b = self.player_by_num(expression.command.data[2].value)
                return (await a.zone_name()) == (await b.zone_name())
            case _:
                raise VMError(f"Unimplemented expression: {expression}")

    async def eval(self, expression: Expression, client: Client | None = None):
        match expression:
            case CommandExpression():
                return await self._eval_command_expression(expression)
            case NumberExpression():
                return expression.number
            case XYZExpression():
                return XYZ(
                    await self.eval(expression.x, client), # type: ignore
                    await self.eval(expression.y, client), # type: ignore
                    await self.eval(expression.z, client), # type: ignore
                )
            case UnaryExpression():
                match expression.operator.kind:
                    case TokenKind.minus:
                        return -(await self.eval(expression.expr, client)) # type: ignore
                    case _:
                        raise VMError(f"Unimplemented unary expression: {expression}")
            case StringExpression():
                return expression.string
            case KeyExpression():
                return expression.key
            case _:
                raise VMError(f"Unimplemented expression type: {expression}")

    async def exec_deimos_call(self, instruction: Instruction):
        assert instruction.kind == InstructionKind.deimos_call
        assert type(instruction.data) == list

        selector: PlayerSelector = instruction.data[0]
        clients = self._select_players(selector)
        # TODO: is eval always fast enough to run in order during a TaskGroup
        match instruction.data[1]:
            case "teleport":
                args = instruction.data[2]
                assert type(args) == list
                assert type(args[0]) == TeleportKind
                async with asyncio.TaskGroup() as tg:
                    match args[0]:
                        case TeleportKind.position:
                            for client in clients:
                                pos: XYZ = await self.eval(args[1], client) # type: ignore
                                tg.create_task(client.teleport(pos))
                        case TeleportKind.entity_literal:
                            name = args[-1]
                            for client in clients:
                                tg.create_task(client.tp_to_closest_by_name(name))
                        case TeleportKind.entity_vague:
                            vague = args[-1]
                            for client in clients:
                                tg.create_task(client.tp_to_closest_by_vague_name(vague))
                        case TeleportKind.mob:
                            for client in clients:
                                tg.create_task(client.tp_to_closest_mob())
                        case TeleportKind.quest:
                            # TODO: "quest" could instead be treated as an XYZ expression or something
                            for client in clients:
                                pos = await client.quest_position.position()
                                tg.create_task(client.teleport(pos))
                        case TeleportKind.friend_icon:
                            async def proxy(client: SprintyClient): # type: ignore
                                # probably doesn't need mouseless
                                async with client.mouse_handler:
                                    await teleport_to_friend_from_list(client, icon_list=2, icon_index=0)
                            for client in clients:
                                tg.create_task(proxy(client))
                        case TeleportKind.friend_name:
                            name = args[-1]
                            async def proxy(client: SprintyClient): # type: ignore
                                async with client.mouse_handler:
                                    await teleport_to_friend_from_list(client, name=name)
                            for client in clients:
                                tg.create_task(proxy(client))
                        case _:
                            raise VMError(f"Unimplemented teleport kind: {instruction}")
            case "goto":
                args = instruction.data[2]
                assert type(args) == list
                async with asyncio.TaskGroup() as tg:
                    for client in clients:
                        pos: XYZ = await self.eval(args[0], client) # type: ignore
                        tg.create_task(client.goto(pos.x, pos.y))
            case "waitfor":
                args = instruction.data[2]
                completion: bool = args[-1]
                assert type(completion) == bool

                async def waitfor_coro(coro, invert: bool, interval=0.25):
                    while not (invert ^ await coro()):
                        await asyncio.sleep(interval)

                async def waitfor_impl(coro, interval=0.25):
                    nonlocal completion
                    await waitfor_coro(coro, False, interval)
                    if completion:
                        await waitfor_coro(coro, True, interval)

                method_map = {
                    WaitforKind.dialog: Client.is_in_dialog,
                    WaitforKind.battle: Client.in_battle,
                    WaitforKind.free: is_free,
                }
                if args[0] in method_map:
                    method = method_map[args[0]]
                    async with asyncio.TaskGroup() as tg:
                        for client in clients:
                            async def proxy(): # type: ignore
                                return await method(client)
                            tg.create_task(waitfor_impl(proxy))
                else:
                    match args[0]:
                        case WaitforKind.zonechange:
                            async with asyncio.TaskGroup() as tg:
                                for client in clients:
                                    starting_zone = await client.zone_name()
                                    async def proxy():
                                        return starting_zone != (await client.zone_name())
                                    tg.create_task(waitfor_coro(proxy, False))
                            if completion:
                                async with asyncio.TaskGroup() as tg:
                                    for client in clients:
                                        tg.create_task(waitfor_coro(client.is_loading, True))
                        case WaitforKind.window:
                            window_path = args[1]
                            async with asyncio.TaskGroup() as tg:
                                for client in clients:
                                    async def proxy():
                                        return await is_visible_by_path(client, window_path)
                                    tg.create_task(waitfor_impl(proxy))
                        case _:
                            raise VMError(f"Unimplemented waitfor kind: {instruction}")
            case _:
                raise VMError(f"Unimplemented deimos call: {instruction}")

    async def step(self):
        if not self.running:
            return
        instruction = self.program[self._ip]
        match instruction.kind:
            case InstructionKind.kill:
                self.kill()
                logger.debug("Bot Killed")
            case InstructionKind.sleep:
                assert instruction.data != None
                time = await self.eval(instruction.data)
                assert type(time) is float
                await asyncio.sleep(time)
                self._ip += 1
            case InstructionKind.jump:
                assert type(instruction.data) == int
                self._ip += instruction.data
            case InstructionKind.jump_if:
                assert type(instruction.data) == list
                if await self.eval(instruction.data[0]):
                    self._ip += instruction.data[1]
                else:
                    self._ip += 1
            case InstructionKind.jump_ifn:
                assert type(instruction.data) == list
                if await self.eval(instruction.data[0]):
                    self._ip += 1
                else:
                    self._ip += instruction.data[1]

            case InstructionKind.call:
                self._callstack.append(self._ip + 1)
                j = self._ip
                label = instruction.data
                # TODO: Less hacky solution. This scans upwards looking for labels
                while True:
                    j -= 1
                    if j < 0:
                        raise VMError(f"Unable to find label: {label}")
                    x = self.program[j]
                    if x.kind != InstructionKind.label or x.data != label:
                        continue
                    break
                self._ip = j
            case InstructionKind.ret:
                self._ip = self._callstack.pop()

            case InstructionKind.log_literal:
                assert type(instruction.data) == list
                strs = []
                for x in instruction.data:
                    match x.kind:
                        case TokenKind.string:
                            strs.append(x.value)
                        case TokenKind.identifier:
                            strs.append(x.literal)
                        case _:
                            raise VMError(f"Unable to log: {x}")
                s = " ".join(strs)
                logger.debug(s)
                self._ip += 1
            case InstructionKind.log_window:
                assert type(instruction.data) == list
                clients = self._select_players(instruction.data[0])
                path = instruction.data[1]
                async with asyncio.TaskGroup() as tg:
                    for client in clients:
                        window = await get_window_from_path(client.root_window, path)
                        if not window:
                            raise VMError(f"Unable to find window at path: {path}")
                        window_str = await window.maybe_text()
                        logger.debug(f"{client.title} - {window_str}")
                self._ip += 1
            case InstructionKind.label:
                self._ip += 1

            case InstructionKind.deimos_call:
                await self.exec_deimos_call(instruction)
                self._ip += 1
            case _:
                raise VMError(f"Unimplemented instruction: {instruction}")
        if self._ip >= len(self.program):
            self.stop()

    async def run(self):
        self.running = True
        while self.running:
            await self.step()
