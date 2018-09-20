// sed - A maubot plugin to do sed-like replacements.
// Copyright (C) 2018 Tulir Asokan
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU Affero General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU Affero General Public License for more details.
//
// You should have received a copy of the GNU Affero General Public License
// along with this program.  If not, see <https://www.gnu.org/licenses/>. 

package main

import (
	"fmt"
	"regexp"
	"strings"
	"sync"

	"maubot.xyz"
	"maunium.net/go/gomatrix"
)

type Sed struct {
	client         maubot.MatrixClient
	log            maubot.Logger
	prevEventLock  sync.RWMutex
	prevEventMap   map[string]map[string]string
	prevEventQueue map[string]*EventQueue
}

func NewEventQueue() *EventQueue {
	return &EventQueue{
		List: make([]*maubot.Event, 10),
		Ptr:  0,
	}
}

type EventQueue struct {
	List []*maubot.Event
	Ptr  int
}

func (eq *EventQueue) Add(evt *maubot.Event) {
	eq.List[eq.Ptr] = evt
	eq.Ptr = (eq.Ptr + 1) % len(eq.List)
}

const (
	CommandShortSed = "net.maunium.sed.short"
	CommandLongSed  = "net.maunium.sed.long"
)

func (bot *Sed) Start() {
	bot.client.SetCommandSpec(&maubot.CommandSpec{
		PassiveCommands: []maubot.PassiveCommand{{
			Name:         CommandShortSed,
			Matches:      `^s([#/])(.*?[^\\]?)[#/](.*?[^\\]?)(?:[#/]([gi]+)?)?$`,
			MatchAgainst: maubot.MatchAgainstBody,
		}, {
			Name:         CommandLongSed,
			Matches:      `sed s(.)(.*?[^\\]?)\1(.*?[^\\]?)\1([gi]+)?`,
			MatchAgainst: maubot.MatchAgainstBody,
		}},
	})
	bot.client.AddEventHandler(gomatrix.EventMessage, bot.MessageHandler)
}

func (bot *Sed) Stop() {}

type SedStatement struct {
	Find    *regexp.Regexp
	Replace string
	Global  bool
}

func (bot *Sed) ParseEvent(evt *maubot.Event) (*SedStatement, error) {
	sed, err := bot.compilePassiveStatement(evt)
	if err != nil {
		return nil, err
	} else if sed != nil {
		return sed, nil
	}

	finder := bot.findFullStatement(evt)
	if finder != nil {
		sed, err := bot.compileStatement(evt, finder)
		if err != nil {
			return nil, err
		} else if sed != nil {
			return sed, nil
		}
	}
	return nil, nil
}

func (bot *Sed) findFullStatement(evt *maubot.Event) *regexp.Regexp {
	index := strings.Index(strings.ToLower(evt.Content.Body), "sed s")
	if index == -1 || index+len("sed s")+3 > len(evt.Content.Body) {
		return nil
	}

	separator := evt.Content.Body[index+len("sed s")]
	regexFinder, _ := regexp.Compile(fmt.Sprintf(`sed s%[1]s(.*?[^\\]?)%[1]s(.*?[^\\]?)%[1]s([gi]+)?`, regexp.QuoteMeta(string(separator))))
	return regexFinder
}

func (bot *Sed) compilePassiveStatement(evt *maubot.Event) (*SedStatement, error) {
	if evt.Unsigned.PassiveCommand == nil {
		return nil, nil
	}
	var matchedCommand *gomatrix.MatchedPassiveCommand
	var ok bool
	if matchedCommand, ok = evt.Unsigned.PassiveCommand[CommandShortSed]; !ok {
		if matchedCommand, ok = evt.Unsigned.PassiveCommand[CommandLongSed]; !ok {
			return nil, nil
		}
	}
	captured := matchedCommand.Captured
	if len(captured) == 0 || len(captured[0]) != 6 {
		return nil, nil
	}
	match := captured[0]

	regex, err := regexp.Compile(match[3])
	if err != nil {
		return nil, fmt.Errorf("failed to compile regex: %v", err)
	}

	flags := match[5]

	return &SedStatement{
		Find:    regex,
		Replace: match[4],
		Global:  strings.ContainsRune(flags, 'g'),
	}, nil
}

func (bot *Sed) compileStatement(evt *maubot.Event, finder *regexp.Regexp) (*SedStatement, error) {
	match := finder.FindStringSubmatch(evt.Content.Body)
	bot.log.Debugln(evt.Content.Body, "---", finder, "---", match)
	if len(match) != 4 {
		return nil, nil
	}

	regex, err := regexp.Compile(match[1])
	if err != nil {
		return nil, fmt.Errorf("failed to compile regex: %v", err)
	}

	flags := match[3]

	return &SedStatement{
		Find:    regex,
		Replace: match[2],
		Global:  strings.ContainsRune(flags, 'g'),
	}, nil
}

func (sed *SedStatement) Exec(body string) string {
	if sed.Global {
		return sed.Find.ReplaceAllString(body, sed.Replace)
	} else {
		replacedOne := false
		return sed.Find.ReplaceAllStringFunc(body, func(match string) string {
			if replacedOne {
				return match
			}
			replacedOne = true
			return sed.Find.ReplaceAllString(match, sed.Replace)
		})
	}
}

func (bot *Sed) RegisterPrevEvent(evt *maubot.Event) {
	bot.prevEventLock.Lock()
	roomMap, ok := bot.prevEventMap[evt.RoomID]
	if !ok {
		roomMap = make(map[string]string)
		bot.prevEventMap[evt.RoomID] = roomMap
	}
	roomMap[evt.Sender] = evt.ID

	roomPrevEventQueue, ok := bot.prevEventQueue[evt.RoomID]
	if !ok {
		roomPrevEventQueue = NewEventQueue()
		bot.prevEventQueue[evt.RoomID] = roomPrevEventQueue
	}
	roomPrevEventQueue.Add(evt)

	bot.prevEventLock.Unlock()
}

func (bot *Sed) GetPrevEvent(roomID, userID string) *maubot.Event {
	bot.prevEventLock.RLock()
	roomMap, ok := bot.prevEventMap[roomID]
	if !ok {
		return nil
	}

	eventID, ok := roomMap[userID]
	if !ok {
		return nil
	}
	bot.prevEventLock.RUnlock()

	return bot.client.GetEvent(roomID, eventID)
}

func (bot *Sed) TryReplaceEvent(sed *SedStatement, evt, origEvt *maubot.Event) bool {
	replaced := sed.Exec(origEvt.Content.Body)
	if replaced == origEvt.Content.Body {
		return false
	}
	origEvt.Reply(replaced)
	return true
}

func (bot *Sed) TryReplaceRecentEvent(sed *SedStatement, evt *maubot.Event) bool {
	bot.prevEventLock.RLock()
	roomPrevEventQueue, ok := bot.prevEventQueue[evt.RoomID]
	if !ok {
		return false
	}
	bot.prevEventLock.RUnlock()

	origPtr := roomPrevEventQueue.Ptr
	listLen := len(roomPrevEventQueue.List)
	for i := listLen; i > 0; i-- {
		origEvt := roomPrevEventQueue.List[(i+origPtr)%listLen]
		if origEvt != nil && bot.TryReplaceEvent(sed, evt, origEvt) {
			return true
		}
	}
	return false
}

func (bot *Sed) MessageHandler(evt *maubot.Event) maubot.EventHandlerResult {
	defer bot.RegisterPrevEvent(evt)

	sed, err := bot.ParseEvent(evt)
	if sed == nil {
		return maubot.Continue
	} else if err != nil {
		evt.Reply(err.Error())
		return maubot.StopEventPropagation
	}

	var origEvt *maubot.Event
	if len(evt.Content.GetReplyTo()) > 0 {
		origEvt = bot.client.GetEvent(evt.RoomID, evt.Content.GetReplyTo())
	} else {
		origEvt = bot.GetPrevEvent(evt.RoomID, evt.Sender)
	}
	evt.MarkRead()

	if origEvt == nil || !bot.TryReplaceEvent(sed, evt, origEvt) {
		if !bot.TryReplaceRecentEvent(sed, evt) {
			return maubot.Continue
		}
	}
	return maubot.StopEventPropagation
}

var Plugin = maubot.PluginCreator{
	Create: func(client maubot.MatrixClient, logger maubot.Logger) maubot.Plugin {
		return &Sed{
			client:         client,
			log:            logger,
			prevEventMap:   make(map[string]map[string]string),
			prevEventQueue: make(map[string]*EventQueue),
		}
	},
	Name:    "maubot.xyz/sed",
	Version: "0.1.0",
}
