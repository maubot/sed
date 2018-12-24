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

from mautrix.types import UserID, RoomID, EventType
from maubot import Plugin, MessageEvent
from maubot.handlers import event, command

EVENT_CACHE_LENGTH = 10

SedStatement = NamedTuple("SedStatement", find=Pattern, replace=str, is_global=bool)
SedMatch = Tuple[str, str, str, str, str]


class SedBot(Plugin):
    prev_user_events: Dict[RoomID, Dict[UserID, MessageEvent]]
    prev_room_events: Dict[RoomID, Deque[MessageEvent]]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prev_user_events = {}
        self.prev_room_events = {}

    @staticmethod
    def _parse_flags(flags: str) -> Tuple[re.RegexFlag, bool]:
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

    def _compile_passive_statement(self, match: SedMatch) -> Optional[SedStatement]:
        if not match or len(match) != 5:
            return None

        flags, is_global = self._parse_flags(match[4] or "")
        return SedStatement(re.compile(match[2], flags), match[3], is_global)

    @staticmethod
    def _exec(stmt: SedStatement, body: str) -> str:
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

    @event.on(EventType.ROOM_MESSAGE)
    async def message_handler(self, evt: MessageEvent) -> None:
        self._register_prev_event(evt)

    @command.passive(r"sed s(.)(.*?[^\\]?)\1(.*?[^\\]?)\1([gi]+)?")
    @command.passive(r"^s([#/])(.*?[^\\]?)\1(.*?[^\\]?)(?:\1([gi]+)?)?$")
    async def command_handler(self, evt: MessageEvent, match: SedMatch) -> None:
        stmt = self._compile_passive_statement(match)
        if not stmt:
            return
        if evt.content.get_reply_to():
            orig_evt = await self.client.get_event(evt.room_id, evt.content.get_reply_to())
        else:
            orig_evt = self.prev_user_events.get(evt.room_id, {}).get(evt.sender, None)
        await evt.mark_read()

        ok = orig_evt and await self._try_replace_event(stmt, orig_evt)
        if ok:
            return

        for recent_event in self.prev_room_events.get(evt.room_id, []):
            if await self._try_replace_event(stmt, recent_event):
                break
