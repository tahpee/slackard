#!/usr/bin/env python

from __future__ import print_function

from glob import glob
import functools
import importlib
import os.path
import re
import slacker
import sys
import time
import yaml
import logging
import datetime


class SlackardFatalError(Exception):
    pass


class SlackardNonFatalError(Exception):
    pass


class Config(object):
    config = {}

    def __init__(self, file_):
        self.file = file_
        f = open(file_, 'r')
        y = yaml.load(f)
        f.close()
        self.__dict__.update(y)


class Slackard(object):

    subscribers = []
    commands = []
    firehoses = []
    timed_tasks = []

    def __init__(self, config_file):
        self.config = Config(config_file)
        self.apikey = self.config.slackard['apikey']
        self.botname = self.config.slackard['botname']
        self.botnick = self.config.slackard['botnick']
        self.channel = self.config.slackard['channel']
        self.plugins = self.config.slackard['plugins']
        self.topic = self.config.slackard['topic']
        try:
            self.boticon = self.config.slackard['boticon']
        except:
            self.boticon = None
        try:
            self.botemoji = ':{0}:'.format(self.config.slackard['botemoji'])
        except:
            self.botemoji = None

    def __str__(self):
        return 'I am a Slackard!'

    def _import_plugins(self):
        logging.debug("Importing plugins...")
        self._set_import_path()
        plugin_prefix = os.path.split(self.plugins)[-1]

        # Import the plugins submodule (however named) and set the
        # bot object in it to self
        importlib.import_module(plugin_prefix)
        sys.modules[plugin_prefix].bot = self

        for plugin in glob('{}/[!_]*.py'.format(self._get_plugin_path())):
            module = '.'.join((plugin_prefix, os.path.split(plugin)[-1][:-3]))
            logging.debug("Importing module %s" % (module))
            importlib.import_module(module)

    def _get_plugin_path(self):
        path = self.plugins
        cf = self.config.file
        if path[0] != '/':
            path = os.path.join(os.path.dirname(os.path.realpath(cf)), path)
        return path

    def _set_import_path(self):
        path = self._get_plugin_path()
        # Use the parent directory of plugin path
        path = os.path.dirname(path)
        if path not in sys.path:
            sys.path = [path] + sys.path

    def _init_connection(self):
        self.slack = slacker.Slacker(self.apikey)
        try:
            r = self.slack.channels.list()
        except slacker.Error as e:
            if e.message == 'invalid_auth':
                raise SlackardFatalError('Invalid API key')
            raise
        except Exception as e:
            raise SlackardNonFatalError(e.message)

        c_map = {c['name']: c['id'] for c in r.body['channels']}
        self.chan_id = c_map[self.channel]

    def _fetch_messages_since(self, oldest=None):
        h = self.slack.channels.history(self.chan_id, oldest=oldest)
        assert(h.successful)
        messages = h.body['messages']
        messages.reverse()
        return [m for m in messages if m['ts'] != oldest]

    def speak(self, message, paste=False):
        if paste:
            message = '```{0}```'.format(message)
        self.slack.chat.post_message(self.chan_id, message,
                                     username=self.botname,
                                     icon_emoji=self.botemoji,
                                     icon_url=self.boticon)

    def upload(self, file, filename=None, title=None):
        if title is None:
            title = ''
        title = '{} (Upload by {})'.format(title, self.botname)
        self.slack.files.upload(file, channels=self.chan_id,
                                filename=filename,
                                title=title)

    def set_topic(self, topic):
        info = self.channel_info()
        if info['topic']['value'] != topic:
            self.slack.channels.set_topic(channel=self.chan_id, topic=topic)

    def channel_info(self):
        info = self.slack.channels.info(channel=self.chan_id)
        return info.body['channel']

    def run_timed_tasks(self):
        logging.debug("Checking for timed tasks")
        now = datetime.datetime.now()
        for task in self.timed_tasks:
            if now.hour >= task['start'] and now.hour <= task['end'] and task['days'][now.weekday()]:
                if task['last'] is None:
                    logging.debug("Running timed task")
                    task['function']()
                    task['last'] = time.time()
                else:
                    delta_t = time.time() - task['last']
                    if delta_t >= task['interval']:
                        task['function']()
                        task['last'] = time.time()

    def run(self):
        self._init_connection()
        self._import_plugins()
        print(self.channel_info())
        self.set_topic(self.topic)

        cmd_matcher = re.compile('^@*{0}:*\s*(\S+)\s*(.*)'.format(
                                 self.botnick), re.IGNORECASE)
        h = self.slack.channels.history(self.chan_id, count=1)
        assert(h.successful)
        t0 = time.time()
        if len(h.body['messages']):
            ts = h.body['messages'][0]['ts']
        else:
            ts = t0

        while True:
            t1 = time.time()
            delta_t = t1 - t0
            if delta_t < 5.0:
                time.sleep(5.0 - delta_t)
            t0 = time.time()

            try:
                messages = self._fetch_messages_since(ts)
            except Exception as e:
                # Possibly an error we can recover from so raise
                # a non-fatal exception and attempt to recover
                raise SlackardNonFatalError(e.message)

            for message in messages:
                ts = message['ts']
                if 'text' in message:
                    # Skip actions on self-produced messages.
                    try:
                        if (message['subtype'] == 'bot_message' and
                                message['username'] == self.botname):
                            continue
                    except KeyError:
                        pass
                    print(message['text'])
                    for f in self.firehoses:
                        f(message['text'])
                    for (f, matcher) in self.subscribers:
                        if matcher.search(message['text']):
                            f(message['text'])

                    m = cmd_matcher.match(message['text'])
                    if m:
                        cmd, args = m.groups()
                        for (f, command) in self.commands:
                            if command == cmd:
                                f(args)
            self.run_timed_tasks()

    def subscribe(self, pattern):
        if hasattr(pattern, '__call__'):
            raise TypeError('Must supply pattern string')

        def real_subscribe(wrapped):
            @functools.wraps(wrapped)
            def _f(*args, **kwargs):
                return wrapped(*args, **kwargs)

            try:
                matcher = re.compile(pattern, re.IGNORECASE)
                self.subscribers.append((_f, matcher))
            except:
                print('Failed to compile matcher for {0}'.format(wrapped))
            return _f

        return real_subscribe

    def command(self, command):
        if hasattr(command, '__call__'):
            raise TypeError('Must supply command string')

        def real_command(wrapped):
            @functools.wraps(wrapped)
            def _f(*args, **kwargs):
                return wrapped(*args, **kwargs)

            self.commands.append((_f, command))
            return _f

        return real_command

    def firehose(self, wrapped):
        @functools.wraps(wrapped)
        def _f(*args, **kwargs):
            return wrapped(*args, **kwargs)

        self.firehoses.append(_f)
        return _f

    def timed_task(self, interval, start=0, end=24, days=(True, True, True, True, True, True, True)):
        def real_command(wrapped):
            @functools.wraps(wrapped)
            def _f(*args, **kwargs):
                return wrapped(*args, **kwargs)
            task = {'function': _f, 'interval': interval, 'last': None, 'start': start, 'end': end, 'days': days}
            self.timed_tasks.append(task)
            return _f
        return real_command


def usage():
    yaml_template = """
    slackard:
        apikey: my_api_key_from-api.slack.com
        channel: random
        botname: Slackard
        botnick: slack  # short form name for commands.
        # Use either boticon or botemoji
        boticon: http://i.imgur.com/IwtcgFm.png
        botemoji: boom
        # plugins directory relative to config file, or absolute
        # create empty __init__.py in that directory
        plugins: ./myplugins
    """
    print('Usage: slackard <config.yaml>')
    print('\nExample YAML\n{}'.format(yaml_template))


def main():
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',)
    # logging.getLogger('').addHandler(logging.StreamHandler())
    config_file = None
    try:
        config_file = sys.argv[1]
    except IndexError:
        pass

    if config_file is None:
        usage()
        sys.exit(1)

    if not os.path.isfile(config_file):
        print('Config file "{}" not found.'.format(config_file))
        sys.exit(1)

    try:
        bot = Slackard(config_file)
    except Exception as e:
        print(e)
        print('Encountered config error: {}'.format(e.message))
        sys.exit(1)

    while True:
        try:
            bot.run()
        except SlackardFatalError as e:
            print('Fatal error: {}'.format(e.message))
            sys.exit(1)
        except SlackardNonFatalError as e:
            print('Non-fatal error: {}'.format(e.message))
            delay = 5
            print('Delaying for {} seconds...'.format(delay))
            time.sleep(delay)
            bot._init_connection()


if __name__ == '__main__':
    main()
