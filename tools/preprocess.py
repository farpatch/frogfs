#!/usr/bin/env python

import csv
from configparser import ConfigParser
import json
import os
import shutil
import subprocess
import sys
from argparse import ArgumentParser
from collections import OrderedDict
from fnmatch import fnmatch

script_dir = os.path.dirname(os.path.realpath(__file__))
used_preprocessors = set()

def load_config(user_config_file=None):
    global config

    defaults_file = os.path.join(script_dir, '..', 'frogfs_defaults.json')
    with open(defaults_file) as f:
        config = json.load(f)
        print("Default config:")
        print(str(config))

    user_config = OrderedDict()
    if user_config_file:
        if not os.path.exists(user_config_file):
            print('{user_config_file} cannot be opened', file=sys.stderr)
            sys.exit(1)
        print("Loading user config file")
        with open(user_config_file) as f:
            user_config = json.load(f)
            print("User config:")
            print(str(user_config))
    else:
        print("Not loading user config file")

    def merge_section(sec_name):
        if sec_name in user_config:
            for subsec_name, subsec in user_config[sec_name].items():
                if subsec is None:
                    if subsec_name in config[sec_name]:
                        del config[sec_name][subsec_name]
                else:
                    if sec_name == 'filters':
                        if isinstance(config[sec_name][subsec_name], str):
                            config[sec_name][subsec_name] = \
                                    [config[sec_name][subsec_name]]
                        if isinstance(subsec, str):
                            subsec = [subsec]
                        config[sec_name][subsec_name] += subsec
                    else:
                        config[sec_name][subsec_name] = subsec

    for sec_name in ('preprocessors', 'compressors', 'filters'):
        merge_section(sec_name)
        for subsec_name, subsec in config.get(sec_name, OrderedDict()).items():
            if isinstance(subsec, str):
                config[sec_name][subsec_name] = [subsec]
            elif isinstance(subsec, dict):
                for subsubsec_name, subsubsec in subsec.items():
                    if isinstance(subsubsec, str):
                        subsec[subsubsec_name] = [subsubsec]
    print(f"Merged config: {json.dumps(config)}")

    class pattern_sort:
        def __init__(self, path, *args):
            self.pattern, _ = path

        def __lt__(self, other):
            if self.pattern == '*':
                return False
            if other.pattern == '*':
                return True
            if self.pattern.startswith('*') and \
                    not other.pattern.startswith('*'):
                return False
            if not self.pattern.startswith('*') and \
                    other.pattern.startswith('*'):
                return True
            return self.pattern < other.pattern

    config['filters'] = OrderedDict(sorted(config['filters'].items(),
            key = pattern_sort))

    preprocessors = list(config['preprocessors'].keys())
    actions = list()
    for action in preprocessors + ['cache', 'discard']:
        actions.append(action)
        actions.append('no-' + action)
    actions += ['skip-preprocessing', 'gzip', 'heatshrink', 'uncompressed']
    config['actions'] = actions

    for filter, actions in config['filters'].items():
        for action in actions:
            if action not in config['actions']:
                print(f"Unknown action `{action}' for filter `{filter}'",
                        file=sys.stderr)
                sys.exit(1)

def get_preprocessors(path):
    global config, used_preprocessors

    preprocessors = OrderedDict()
    for pattern, actions in config['filters'].items():
        if fnmatch(path, pattern):
            for action in actions:
                enable = not action.startswith('no-')
                if not enable:
                    action = action[3:]
                if action in config['preprocessors']:
                    if enable:
                        preprocessors[action] = None
                        used_preprocessors.add(action)
                    else:
                        try:
                            del preprocessors[action]
                        except:
                            pass
                    preprocessors[action] = enable
        if 'skip-preprocessing' in actions:
            return ()

    return tuple(preprocessors)

def get_flags(path):
    global config

    flags = OrderedDict()
    for pattern, actions in config['filters'].items():
        if fnmatch(path, pattern):
            for action in actions:
                enable = not action.startswith('no-')
                if not enable:
                    action = action[3:]
                if action in ('cache', 'discard', 'skip'):
                    flags[action] = enable

    return flags

def get_compressor(path):
    global config

    compressor = 'uncompressed'
    for pattern, actions in config['filters'].items():
        if fnmatch(path, pattern):
            for action in actions:
                if action in ('gzip', 'heatshrink', 'uncompressed'):
                    compressor = action
    return compressor

def load_state(dst_dir):
    state = dict()
    state_file = os.path.join(dst_dir, '.state')
    if os.path.exists(state_file):
        with open(state_file, newline='') as f:
            reader = csv.reader(f, quoting=csv.QUOTE_NONNUMERIC)
            for data in reader:
                path, type, mtime, flags, preprocessors, compressor = data
                state[path] = {
                    'type': type,
                    'mtime': mtime,
                    'preprocessors': () if not preprocessors else \
                            tuple(preprocessors.split(',')),
                    'flags': () if not flags else tuple(flags.split(',')),
                    'compressor': compressor,
                }
    return state

def save_state(dst_dir, state):
    with open(os.path.join(dst_dir, '.state'), 'w', newline='') as f:
        writer = csv.writer(f, quoting=csv.QUOTE_NONNUMERIC)
        for path, data in state.items():
            row = (path, data['type'], data['mtime'],
                    ','.join(data['flags']),
                    ','.join(data['preprocessors']),
                    data['compressor'])
            writer.writerow(row)

    dotconfig = ConfigParser()
    dotconfig['gzip'] = {
        'level': config['compressors']['gzip']['level'],
    }
    dotconfig['heatshrink'] = {
        'window_sz2': config['compressors']['heatshrink']['window_sz2'],
        'lookahead_sz2': config['compressors']['heatshrink']['lookahead_sz2'],
    }
    with open(os.path.join(dst_dir, '.config'), 'w') as f:
        dotconfig.write(f)

def build_state(src_dir):
    state = dict()
    for dir, _, files in os.walk(src_dir, followlinks=True):
        reldir = os.path.relpath(dir, src_dir).replace('\\', '/').lstrip('.') \
                .lstrip('/')
        absdir = os.path.abspath(dir)
        if reldir and os.path.exists(absdir):
            state[reldir] = {
                'type': 'dir',
                'mtime': os.path.getmtime(absdir),
                'preprocessors': (),
                'flags': (),
                'compressor': 'uncompressed',
            }
        for file in files:
            relfile = os.path.join(reldir, file).replace('\\','/').lstrip('/')
            absfile = os.path.join(absdir, file)
            if os.path.exists(absfile):
                state[relfile] = {
                    'type': 'file',
                    'mtime': os.path.getmtime(absfile),
                    'preprocessors': get_preprocessors(relfile),
                    'flags': get_flags(relfile),
                    'compressor': get_compressor(relfile),
                }
    return state

def install_preprocessors(config, root_dir):
    global used_preprocessors

    # Work around a bug in npm -- if `node_modules` doesn't exist it will
    # create one at some random path above. Sometimes. Depending on the version.
    # Except on Tuesdays in Norway.
    node_modules = os.path.join(root_dir.replace('/', os.path.sep), 'node_modules')
    if not os.path.exists(node_modules):
        os.mkdir(node_modules)

    for name in used_preprocessors:
        preprocessor = config['preprocessors'][name]
        if 'install' in preprocessor:
            install = preprocessor['install']
            subprocess.check_call(install, shell=True, cwd=root_dir)
        elif 'npm' in preprocessor:
            for npm in preprocessor['npm']:
                test_path = os.path.join(root_dir.replace('/', os.path.sep),
                                         'node_modules',
                                         npm.replace('/', os.path.sep))
                if not os.path.exists(test_path):
                    subprocess.check_call(f'npm install {npm}', shell=True, cwd=root_dir)

def preprocess(path, preprocessors):
    global config

    src_abs = os.path.join(args.src_dir, path)
    dst_abs = os.path.join(args.dst_dir, path)
    if os.path.isdir(src_abs):
        if os.path.isdir(dst_abs):
            pass
        else:
            os.mkdir(dst_abs)
    else:
        os.makedirs(os.path.dirname(dst_abs), exist_ok=True)

        with open(src_abs, 'rb') as f:
            data = f.read()

        if preprocessors:
            print(f'       - preprocessing {path}', file=sys.stderr)

        for preprocessor in preprocessors:
            print(f'         - running {preprocessor}')
            command = config['preprocessors'][preprocessor]['command']
            if command[0].startswith('tools/'):
                command[0] = os.path.join(script_dir, command[0][6:])
            # These are implemented as `.cmd` files on Windows, which explicitly
            # requires them to be run under `cmd /c`
            if os.name == 'nt':
                command = ["cmd", "/c"] + command
            process = subprocess.Popen(command, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, shell=True)
            data = process.communicate(input=data)[0]

        with open(dst_abs, 'wb') as f:
            f.write(data)

def main():
    global args, config

    parser = ArgumentParser()
    parser.add_argument('src_dir', metavar='SRC', help='source directory')
    parser.add_argument('dst_dir', metavar='DST', help='destination directory')
    parser.add_argument('--config', help='user configuration')
    parser.add_argument('--root', metavar='ROOT', help='build root directory')
    args = parser.parse_args()

    load_config(args.config)

    old_state = load_state(args.dst_dir)
    new_state = build_state(args.src_dir)

    print(f"Root: {args.root}")
    install_preprocessors(config, args.root)

    old_paths = set(old_state.keys())
    new_paths = set(new_state.keys())

    delete_paths = old_paths - new_paths
    copy_paths = new_paths - old_paths
    compare_paths = old_paths & new_paths

    if not delete_paths and not copy_paths and not compare_paths:
        sys.exit(0)

    for path in delete_paths:
        dst_abs = os.path.join(args.dst_dir, path)
        if os.path.exists(dst_abs):
            if os.path.isdir(dst_abs):
                shutil.rmtree(dst_abs, True)
            else:
                os.unlink(dst_abs)

    for path in copy_paths:
        preprocess(path, new_state[path]['preprocessors'])

    changes = bool(delete_paths or copy_paths)
    for path in compare_paths:
        if old_state[path]['type'] != new_state[path]['type'] or \
                old_state[path]['preprocessors'] != \
                        new_state[path]['preprocessors'] or \
                old_state[path]['mtime'] < new_state[path]['mtime']:

            changes = True

            dst_abs = os.path.join(args.dst_dir, path)

            if os.path.exists(dst_abs):
                if os.path.isdir(dst_abs):
                    shutil.rmtree(dst_abs, True)
                else:
                    os.unlink(dst_abs)

            preprocess(path, new_state[path]['preprocessors'])

    if changes:
        save_state(args.dst_dir, new_state)

if __name__ == '__main__':
    main()
