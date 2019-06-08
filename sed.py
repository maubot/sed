# sed - A maubot plugin to do sed-like replacements.
# Copyright (C) 2019 Tulir Asokan
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
from difflib import SequenceMatcher
from html import escape
import string
import re

from mautrix.types import UserID, RoomID, EventType, MessageType, TextMessageEventContent, Format
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
    def _read_until_separator(raw_statement: str, separator: str, require: bool = True
                              ) -> Optional[Tuple[str, str]]:
        value = ""
        while True:
            try:
                sep_index = raw_statement.index(separator)
            except ValueError:
                if require:
                    raise
                return raw_statement, value
            if sep_index == 0:
                return value, raw_statement[1:]
            elif raw_statement[sep_index - 1] == "\\":
                value += raw_statement[:sep_index - 1] + separator
                raw_statement = raw_statement[sep_index + 1:]
            else:
                value += raw_statement[:sep_index]
                raw_statement = raw_statement[sep_index + 1:]
                return value, raw_statement

    @staticmethod
    def _parse_flags(raw_statement: str, allow_unknown_flags: bool = False
                     ) -> Tuple[re.RegexFlag, bool]:
        re_flags = {
            "i": re.IGNORECASE,
            "m": re.MULTILINE,
            "s": re.DOTALL,
            "t": re.TEMPLATE,
        }
        flags = re.UNICODE
        is_global = False
        for char in raw_statement.lower():
            try:
                flags += re_flags[char]
            except KeyError:
                if char == "g":
                    is_global = True
                elif not allow_unknown_flags:
                    raise ValueError(f"Unknown flag {char}")
                elif char not in string.ascii_lowercase:
                    break
        return flags, is_global

    @classmethod
    def _compile_passive_statement(cls, match: SedMatch) -> Optional[SedStatement]:
        if not match or len(match) != 2:
            return None

        full_size = match[0] != match[1]

        raw_statement = match[1]
        if raw_statement[0] != "s":
            return None

        try:
            separator, raw_statement = raw_statement[1], raw_statement[2:]
            if separator not in ("/", "#") and not full_size:
                return None
            regex, raw_statement = cls._read_until_separator(raw_statement, separator)
            replacement, raw_statement = cls._read_until_separator(raw_statement, separator,
                                                                   require=full_size)
            flags, is_global = cls._parse_flags(raw_statement, full_size)
        except ValueError:
            return None
        return SedStatement(re.compile(regex, flags), replacement, is_global)

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

    @staticmethod
    def op_to_str(tag: str, old_text: str, new_text: str) -> str:
        if tag == "equal":
            return new_text
        elif tag == "insert" or tag == "replace":
            return f"<u>{new_text}</u>"
        elif tag == "delete":
            return ""

    @classmethod
    def highlight_edits(cls, new_text: str, old_text: str) -> str:
        matcher = SequenceMatcher(a=old_text, b=new_text)
        return "".join(cls.op_to_str(tag, old_text[old_start:old_end], new_text[new_start:new_end])
                       for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes())

    async def _get_displayname(self, room_id: RoomID, user_id: UserID) -> str:
        event = await self.client.get_state_event(room_id, EventType.ROOM_MEMBER, user_id)
        return event.displayname

    async def _try_replace_event(self, stmt: SedStatement, orig_evt: MessageEvent) -> bool:
        replaced = self._exec(stmt, orig_evt.content.body)
        if replaced == orig_evt.content.body:
            return False
        content = TextMessageEventContent(
            msgtype=MessageType.NOTICE, body=replaced, format=Format.HTML,
            formatted_body=self.highlight_edits(replaced, orig_evt.content.body))
        if orig_evt.content.msgtype == MessageType.EMOTE:
            displayname = await self._get_displayname(orig_evt.room_id, orig_evt.sender)
            content.body = f"* {displayname} {content.body}"
            content.formatted_body = f"* {escape(displayname)} {content.formatted_body}"
        await orig_evt.reply(content)
        return True

    @event.on(EventType.ROOM_MESSAGE)
    async def message_handler(self, evt: MessageEvent) -> None:
        self._register_prev_event(evt)

    @command.passive(r"(?:^|[^a-zA-Z0-9])sed (s.+)")
    @command.passive(r"^(s[#/].+[#/].+)$")
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
