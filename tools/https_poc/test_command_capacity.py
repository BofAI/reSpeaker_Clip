"""Preprocess actual registrations, including the resource command registered last."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT/'applications/clip/src'

def preprocess(name, options):
    source = '\n'.join(x for x in (SRC/name).read_text().splitlines()
                       if not x.lstrip().startswith('#include'))
    return subprocess.check_output(['clang', '-E', '-P', '-x', 'c', '-'] +
                                   ['-D'+x+'=1' for x in options], input=source, text=True)

class CommandCapacityTest(unittest.TestCase):
    def test_baseline_and_all_diagnostics_fit_registry(self):
        common = ['CONFIG_CLIP_HTTPS_POC', 'CONFIG_CLIP_RESOURCE_STATS',
                  'CONFIG_CLIP_WIFI_DIAG', 'CONFIG_CLIP_HTTPS_TUNING',
                  'CONFIG_CLIP_TCP_TRACE', 'CONFIG_CLIP_HTTPS_PROFILE']
        for extra in ([], ['CONFIG_CLIP_HTTPS_EXPERIMENT'],
                      ['CONFIG_CLIP_UPLOAD_BENCH', 'CONFIG_CLIP_UPLOAD_BENCH_CACHE',
                       'CONFIG_CLIP_UPLOAD_BENCH_DTLS']):
            options = common + extra
            bench = 'CONFIG_CLIP_UPLOAD_BENCH' in options
            stock = preprocess(ROOT / 'applications/upload_bench/src/main.c'
                               if bench else 'at_commands.c', options)
            if bench:
                commands = stock.split('static const struct at_command commands[]', 1)[1].split('};', 1)[0]
                count = len(re.findall(r'\{\s*"', commands))
            else:
                count = len(re.findall(r'at_server_register_cmd\(&\w+_cmd\)', stock))
            self.assertIn('resource_stats_register()', stock)
            count += 1
            https = preprocess('https_poc.c', options)
            commands = https.split('static const struct at_command commands[] = {', 1)[1].split('};', 1)[0]
            count += len(re.findall(r'\{\s*"', commands))
            diag = preprocess('wifi_diag.c', options)
            count += len(re.findall(r'at_server_register_cmd\(&\w+\)', diag))
            registry = preprocess('at_server.c', options)
            capacity = int(re.search(r'\*cmds\[(\d+)\]', registry)[1])
            with self.subTest(experiment=bool(extra), commands=count, capacity=capacity):
                self.assertLessEqual(count, capacity)
