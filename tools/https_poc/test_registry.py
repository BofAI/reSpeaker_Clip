"""Registration capacity must cover the actual compiled feature command names."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class RegistryTest(unittest.TestCase):
    def test_all_experimental_and_stock_commands_register_before_startup(self):
        source = (ROOT/'applications/clip/src/at_server.c').read_text()
        registry = source.split('/* Command registry */', 1)[1].split('/* Forward declarations */', 1)[0]
        register = 'int at_server_register_cmd' + source.split('int at_server_register_cmd', 1)[1].split('/* Skip whitespace */', 1)[0]
        stock = (ROOT/'applications/clip/src/at_commands.c').read_text().split('int at_commands_register(void)', 1)[1]
        names = re.findall(r'\.name\s*=\s*"([A-Z]+)"', stock)
        https = (ROOT/'applications/clip/src/https_poc.c').read_text().split('static const struct at_command commands[]', 1)[1].split('};', 1)[0]
        names += re.findall(r'\{"([A-Z]+)"', https)
        diag = (ROOT/'applications/clip/src/wifi_diag.c').read_text()
        names += re.findall(r'\{\s*"([A-Z]+)"', diag)
        # resource_stats uses a positional initializer.
        resources = (ROOT/'applications/clip/src/resource_stats.c').read_text()
        self.assertIn('"RES"', resources)
        names += ['RES']
        self.assertEqual(len(names), len(set(names)))
        self.assertGreater(len(names), 40)
        code = '''
#include <assert.h>
#include <stddef.h>
#include <errno.h>
#define CONFIG_CLIP_HTTPS_POC 1
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define LOG_DBG(...) ((void)0)
struct at_command {const char *name; void (*handler)(void);};
static void handler(void) {}
'''
        code += registry + register
        code += '\nint main(void) {\nstatic const char *names[]={' + ','.join('"'+n+'"' for n in names) + '};\n'
        code += '''
 struct at_command commands[ARRAY_SIZE(names)];
 for (size_t i=0;i<ARRAY_SIZE(names);i++) {
  commands[i]=(struct at_command){names[i],handler};
  assert(at_server_register_cmd(&commands[i])==0);
 }
 assert(cmd_registry.count==(int)ARRAY_SIZE(names));
 struct at_command extra={"EXTRA",handler};
 while (cmd_registry.count<(int)ARRAY_SIZE(cmd_registry.cmds)) assert(at_server_register_cmd(&extra)==0);
 assert(at_server_register_cmd(&extra)==-ENOMEM);
 assert(at_server_register_cmd(NULL)==-EINVAL);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'test.c').write_text(code)
            subprocess.run(['clang','-std=c11','-Wall','-Wextra','-Werror',
                            '-fsanitize=address,undefined',str(root/'test.c'),'-o',str(root/'test')],check=True)
            subprocess.run([str(root/'test')],check=True)
