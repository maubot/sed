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

	"maubot.xyz"
)

type Sed struct {
	client maubot.MatrixClient
}

func (bot *Sed) Start() {
	bot.client.AddEventHandler("m.room.message", bot.MessageHandler)
}

func (bot *Sed) Stop() {}

type SedStatement struct {
	Find    *regexp.Regexp
	Replace string
	Global  bool
}

func (bot *Sed) ParseEvent(evt *maubot.Event) (*SedStatement, error) {
	finder := bot.findShortStatement(evt)
	if finder != nil {
		sed, err := bot.compileStatement(evt, finder)
		if err != nil {
			return nil, err
		} else if sed != nil {
			return sed, nil
		}
	}

	finder = bot.findFullStatement(evt)
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
	regexFinder, _ := regexp.Compile(fmt.Sprintf(`sed s%[1]s(.*?[^\\])%[1]s(.*?[^\\]?)%[1]s([gi]+)?`, regexp.QuoteMeta(string(separator))))
	return regexFinder
}

func (bot *Sed) findShortStatement(evt *maubot.Event) *regexp.Regexp {
	if len(evt.Content.Body) < 4 || evt.Content.Body[0] != 's' {
		return nil
	}

	separator := evt.Content.Body[1]
	regexFinder, _ := regexp.Compile(fmt.Sprintf(`^s%[1]s(.*?[^\\])%[1]s(.*?[^\\]?)%[1]s([gi]+)?$`, regexp.QuoteMeta(string(separator))))
	return regexFinder
}

func (bot *Sed) compileStatement(evt *maubot.Event, finder *regexp.Regexp) (*SedStatement, error) {
	match := finder.FindStringSubmatch(evt.Content.Body)
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

func (bot *Sed) MessageHandler(evt *maubot.Event) bool {
	sed, err := bot.ParseEvent(evt)
	if sed == nil {
		return true
	} else if err != nil {
		evt.Reply(err.Error())
		return false
	}

	evt.MarkRead()

	origEvt := bot.client.GetEvent(evt.RoomID, evt.Content.RelatesTo.InReplyTo.EventID)
	if origEvt == nil {
		evt.Reply("Failed to load event to replace")
		return true
	}

	replaced := sed.Exec(origEvt.Content.Body)
	origEvt.Reply(replaced)
	return false
}

var Plugin = maubot.PluginCreator{
	Create: func(client maubot.MatrixClient) maubot.Plugin {
		return &Sed{
			client: client,
		}
	},
	Name:    "maubot.xyz/sed",
	Version: "0.1.0",
}
