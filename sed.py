# sed - A maubot plugin to do sed-like replacements.
# Copyright (C) 2018 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from typing import Tuple, NamedTuple, Pattern, Optional, Dict, Deque
from collections import deque
import re

from maubot import Plugin, CommandSpec, PassiveCommand
from mautrix.types import MessageEvent, UserID, RoomID, EventType

COMMAND_SHORT_SED = "xyz.maubot.sed.short"
COMMAND_LONG_SED = "xyz.maubot.sed.long"

EVENT_CACHE_LENGTH = 10

SedStatement = NamedTuple("SedStatement", find=Pattern, replace=str, is_global=bool)


class SedBot(Plugin):
    prev_user_events: Dict[RoomID, Dict[UserID, MessageEvent]]
    prev_room_events: Dict[RoomID, Deque[MessageEvent]]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prev_user_events = {}
        self.prev_room_events = {}

    async def start(self) -> None:
        self.set_command_spec(CommandSpec(
            passive_commands=[PassiveCommand(
                name=COMMAND_SHORT_SED,
                matches=r"^s([#/])(.*?[^\\]?)[#/](.*?[^\\]?)(?:[#/]([gi]+)?)?$",
                match_against="body",
            ), PassiveCommand(
                name=COMMAND_LONG_SED,
                matches=r"sed s(.)(.*?[^\\]?)\1(.*?[^\\]?)\1([gi]+)?",
                match_against="body",
            )],
        ))
        self.client.add_command_handler(COMMAND_SHORT_SED, self.command_handler)
        self.client.add_command_handler(COMMAND_LONG_SED, self.command_handler)
        self.client.add_event_handler(self.message_handler, EventType.ROOM_MESSAGE)

    def _parse_flags(self, flags: str) -> Tuple[re.RegexFlag, bool]:
        re_flags = {
            "i": re.IGNORECASE,
            "m": re.MULTILINE,
            "s": re.DOTALL,
            "t": re.TEMPLATE,
        }
        flag = re.UNICODE
        is_global = False
        for char in flags.lower():
            try:
                flag += re_flags[char]
            except KeyError:
                if char == "g":
                    is_global = True
        return flag, is_global

    def _compile_passive_statement(self, evt: MessageEvent) -> Optional[SedStatement]:
        if not evt.unsigned.passive_command:
            return None
        command = (evt.unsigned.passive_command.get(COMMAND_SHORT_SED, None)
                   or evt.unsigned.passive_command.get(COMMAND_LONG_SED, None))
        if not command:
            return None

        if len(command.captured) == 0 or len(command.captured[0]) != 5:
            return None

        match = command.captured[0]

        flags, is_global = self._parse_flags(match[4] or "")
        return SedStatement(re.compile(match[2], flags), match[3], is_global)

    def _exec(self, stmt: SedStatement, body: str) -> str:
        return stmt.find.sub(stmt.replace, body, count=0 if stmt.is_global else 1)

    def _register_prev_event(self, evt: MessageEvent) -> None:
        try:
            events = self.prev_room_events[evt.room_id]
        except KeyError:
            self.prev_room_events[evt.room_id] = events = deque()
        events.appendleft(evt)
        if len(events) > EVENT_CACHE_LENGTH:
            events.pop()

        self.prev_user_events.setdefault(evt.room_id, {})[evt.sender] = evt

    async def _try_replace_event(self, stmt: SedStatement, orig_evt: MessageEvent) -> bool:
        replaced = self._exec(stmt, orig_evt.content.body)
        if replaced == orig_evt.content.body:
            return False
        await orig_evt.reply(replaced)
        return True

    async def message_handler(self, evt: MessageEvent) -> None:
        self._register_prev_event(evt)

    async def command_handler(self, evt: MessageEvent) -> None:
        print(evt.content.body)
        print(evt.unsigned.passive_command)
        stmt = self._compile_passive_statement(evt)
        if not stmt:
            return
        if evt.content.get_reply_to():
            orig_evt = await self.client.get_event(evt.room_id, evt.content.get_reply_to())
        else:
            orig_evt = self.prev_user_events.get(evt.room_id, {}).get(evt.sender, None)
        print(orig_evt)
        await evt.mark_read()

        ok = orig_evt and await self._try_replace_event(stmt, orig_evt)
        if ok:
            return

        for recent_event in self.prev_room_events.get(evt.room_id, []):
            if await self._try_replace_event(stmt, recent_event):
                break
