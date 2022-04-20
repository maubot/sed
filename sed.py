# sed - A maubot plugin to do sed-like replacements.
# Copyright (C) 2022 Tulir Asokan
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
from types import FrameType
from collections import deque, defaultdict
from contextlib import contextmanager
from difflib import SequenceMatcher
from html import escape
import string
import signal
import time
import re

from mautrix.types import (UserID, RoomID, EventID, EventType, MessageType, TextMessageEventContent,
                           Format, RedactionEvent, RelatesTo, RelationType)
from maubot import Plugin, MessageEvent
from maubot.handlers import event, command

EVENT_CACHE_LENGTH = 10

SedStatement = NamedTuple("SedStatement", find=Pattern, replace=str, is_global=bool,
                          highlight_edits=bool)
HistoricalSed = NamedTuple("HistoricalSed", seds_event=EventID, output_event=EventID)

SedMatch = Tuple[str, str, str, str, str]


def raise_timeout(sig: signal.Signals, frame_type: FrameType) -> None:
    raise TimeoutError()


@contextmanager
def timeout(max_time: float = 0.5) -> None:
    signal.signal(signal.SIGALRM, raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, max_time)
    try:
        yield
    finally:
        signal.alarm(0)


class SedBot(Plugin):
    prev_user_events: Dict[RoomID, Dict[UserID, MessageEvent]]
    prev_room_events: Dict[RoomID, Deque[MessageEvent]]
    history: Dict[EventID, HistoricalSed]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prev_user_events = defaultdict(lambda: {})
        self.prev_room_events = defaultdict(lambda: deque(maxlen=EVENT_CACHE_LENGTH))
        self.history = {}

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
                     ) -> Tuple[re.RegexFlag, bool, bool]:
        re_flags = {
            "a": re.ASCII,
            "i": re.IGNORECASE,
            "m": re.MULTILINE,
            "s": re.DOTALL,
            "t": re.TEMPLATE,
        }
        flags = re.UNICODE
        is_global = False
        no_underline = False
        for char in raw_statement.lower():
            try:
                flags += re_flags[char]
            except KeyError:
                if char == "g":
                    is_global = True
                elif char == "u":
                    no_underline = True
                elif not allow_unknown_flags:
                    raise ValueError(f"Unknown flag {char}")
                elif char not in string.ascii_lowercase:
                    break
        return re.RegexFlag(flags), is_global, no_underline

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
            flags, is_global, no_underline = cls._parse_flags(raw_statement, full_size)
        except ValueError:
            return None
        return SedStatement(re.compile(regex, flags), replacement, is_global=is_global,
                            highlight_edits=not no_underline)

    @staticmethod
    def _exec(stmt: SedStatement, body: str) -> str:
        with timeout():
            return stmt.find.sub(stmt.replace, body, count=0 if stmt.is_global else 1)

    @staticmethod
    def op_to_str(tag: str, old_text: str, new_text: str) -> str:
        if tag == "equal":
            return new_text
        elif tag == "insert" or tag == "replace":
            return f"<u>{new_text}</u>"
        elif tag == "delete":
            return ""

    @classmethod
    def highlight_edits(cls, new_text: str, old_text: str, highlight: bool) -> str:
        if not highlight:
            return escape(new_text)
        matcher = SequenceMatcher(a=old_text, b=new_text)
        return "".join(cls.op_to_str(tag, old_text[old_start:old_end], new_text[new_start:new_end])
                       for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes())

    async def _get_displayname(self, room_id: RoomID, user_id: UserID) -> str:
        event = await self.client.get_state_event(room_id, EventType.ROOM_MEMBER, user_id)
        return event.displayname

    async def _try_replace_event(self, event_id: EventID, stmt: SedStatement, orig_evt: MessageEvent
                                 ) -> bool:
        replaced = self._exec(stmt, orig_evt.content.body)
        if replaced == orig_evt.content.body:
            return False
        content = TextMessageEventContent(
            msgtype=MessageType.NOTICE, body=replaced, format=Format.HTML,
            formatted_body=self.highlight_edits(replaced, orig_evt.content.body,
                                                stmt.highlight_edits))
        if orig_evt.content.msgtype == MessageType.EMOTE:
            displayname = await self._get_displayname(orig_evt.room_id, orig_evt.sender)
            content.body = f"* {displayname} {content.body}"
            content.formatted_body = f"* {escape(displayname)} {content.formatted_body}"
        output_event = await orig_evt.reply(content)
        self.history[event_id] = HistoricalSed(output_event=output_event,
                                               seds_event=orig_evt.event_id)
        return True

    @event.on(EventType.ROOM_REDACTION)
    async def redaction_handler(self, evt: RedactionEvent) -> None:
        try:
            sed = self.history[evt.redacts]
        except KeyError:
            return
        await self.client.redact(evt.room_id, sed.output_event)

    @event.on(EventType.ROOM_MESSAGE)
    async def message_handler(self, evt: MessageEvent) -> None:
        await self.command_handler(evt)
        self.prev_room_events[evt.room_id].appendleft(evt)
        self.prev_user_events[evt.room_id][evt.sender] = evt

    @staticmethod
    def _is_recent(evt: MessageEvent) -> bool:
        return evt.timestamp + 5 * 60 * 1000 > time.time() * 1000

    @event.off
    @command.passive(r"(?:^|[^a-zA-Z0-9])sed (s.+)")
    @command.passive(r"^(s[#/].+[#/].+)$")
    async def command_handler(self, evt: MessageEvent, match: SedMatch) -> None:
        try:
            await self._command_handler(evt, match)
        except TimeoutError:
            await evt.reply("3:<")

    async def _command_handler(self, evt: MessageEvent, match: SedMatch):
        with timeout():
            stmt = self._compile_passive_statement(match)
        if not stmt:
            return
        try:
            original_sed = self.history[evt.content.get_edit()]
            await self.edit_handler(evt, stmt, original_sed)
            return
        except KeyError:
            pass
        await evt.mark_read()
        if evt.content.get_reply_to():
            reply_evt = await self.client.get_event(evt.room_id, evt.content.get_reply_to())
            await self._try_replace_event(evt.event_id, stmt, reply_evt)
            return

        room_prev_evts = self.prev_room_events.get(evt.room_id, [])
        own_prev_evt = self.prev_user_events.get(evt.room_id, {}).get(evt.sender, None)
        if ((own_prev_evt in room_prev_evts or self._is_recent(own_prev_evt))
                and await self._try_replace_event(evt.event_id, stmt, own_prev_evt)):
            return

        for recent_event in room_prev_evts:
            if await self._try_replace_event(evt.event_id, stmt, recent_event):
                break

    async def edit_handler(self, evt: MessageEvent, stmt: SedStatement, original_sed: HistoricalSed
                           ) -> None:
        orig_evt = await self.client.get_event(evt.room_id, original_sed.seds_event)
        replaced = self._exec(stmt, orig_evt.content.body)
        content = TextMessageEventContent(
            msgtype=MessageType.NOTICE, body=replaced, format=Format.HTML,
            formatted_body=self.highlight_edits(replaced, orig_evt.content.body,
                                                stmt.highlight_edits),
            relates_to=RelatesTo(rel_type=RelationType.REPLACE, event_id=original_sed.output_event))

        if orig_evt.content.msgtype == MessageType.EMOTE:
            displayname = await self._get_displayname(orig_evt.room_id, orig_evt.sender)
            content.body = f"* {displayname} {content.body}"
            content.formatted_body = f"* {escape(displayname)} {content.formatted_body}"
        await self.client.send_message(evt.room_id, content)
