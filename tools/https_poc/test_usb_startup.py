"""Exercise actual USB initialization against early controller power events."""
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[2] / 'applications/clip/src/usb_cdc.c'


def function(source, signature):
    start = source.index(signature)
    brace = source.index('{', start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


class UsbStartupTest(unittest.TestCase):
    def test_cable_present_absent_removed_and_initialization_errors(self):
        source = SOURCE.read_text()
        functions = '\n'.join(function(source, name) for name in (
            'static void usb_timeout_handler(', 'static void usb_msg_cb(',
            'int usb_cdc_init(', 'int usb_cdc_enable(', 'int usb_cdc_disable('))
        fixture = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <errno.h>
#include <string.h>
#define LOG_INF(...) ((void)0)
#define LOG_ERR(...) ((void)0)
#define LOG_WRN(...) ((void)0)
#define K_MSEC(n) (n)
#define USB_NO_VBUS_TIMEOUT_MS 600000
#define IS_ENABLED(v) TEST_MSC
#if TEST_MSC
#define CONFIG_USBD_MSC_CLASS 1
#endif
#define USBD_SPEED_FS 1
#define UART_LINE_CTRL_DTR 1
#define USBD_MSG_VBUS_READY 1
#define USBD_MSG_VBUS_REMOVED 2
#define USBD_MSG_CDC_ACM_CONTROL_LINE_STATE 3
struct k_work {int unused;};
struct k_work_delayable {bool initialized, armed; int delay;};
struct usbd_context {int unused;};
struct device {int unused;};
struct usbd_msg {int type; const struct device *dev;};
static struct usbd_context clip_usbd;
static struct device uart;
static const struct device *cdc_dev=&uart;
static int clip_lang,clip_mfr,clip_product,clip_fs_config;
static bool usb_active,usb_vbus_present;
static struct k_work_delayable usb_timeout_work;
static void (*registered)(struct usbd_context *const,const struct usbd_msg *);
static int register_error,init_error,init_calls,enabled,disabled,remounts;
static bool initially_present;
static int usb_cdc_disable(void);
static void k_work_init_delayable(struct k_work_delayable *w,void (*fn)(struct k_work *))
{(void)fn;w->initialized=true;w->armed=false;}
static int k_work_cancel_delayable(struct k_work_delayable *w)
{assert(w->initialized);w->armed=false;return 0;}
static int k_work_schedule(struct k_work_delayable *w,int delay)
{assert(w->initialized);w->armed=true;w->delay=delay;return 1;}
static int device_is_ready(const struct device *dev){(void)dev;return 1;}
static int usbd_add_descriptor(struct usbd_context *c,int *d){(void)c;(void)d;return 0;}
static int usbd_add_configuration(struct usbd_context *c,int speed,int *d)
{(void)c;(void)speed;(void)d;return 0;}
static int usbd_register_class(struct usbd_context *c,const char *name,int speed,int cfg)
{(void)c;(void)name;(void)speed;(void)cfg;return 0;}
static int usbd_msg_register_cb(struct usbd_context *c,void (*cb)(struct usbd_context *const,const struct usbd_msg *))
{(void)c;if(register_error)return register_error;registered=cb;return 0;}
static int usbd_init(struct usbd_context *c)
{
 init_calls++;
 /* Deliver a real startup ordering hazard: an event during controller init.
  * The old application silently missed it because no callback existed. */
 if(initially_present && registered){struct usbd_msg m={USBD_MSG_VBUS_READY,cdc_dev};registered(c,&m);}
 return init_error;
}
static int usbd_enable(struct usbd_context *c){(void)c;enabled++;return 0;}
static int usbd_disable(struct usbd_context *c){(void)c;disabled++;return 0;}
static void clip_usb_dfu_check(const struct usbd_msg *m){(void)m;}
static int uart_line_ctrl_get(const struct device *d,int kind,uint32_t *v)
{(void)d;(void)kind;*v=1;return 0;}
static void uart_irq_rx_enable(const struct device *d){(void)d;}
static void uart_irq_rx_disable(const struct device *d){(void)d;}
static void cdc_irq_handler(const struct device *d,void *ctx){(void)d;(void)ctx;}
static int uart_irq_callback_set(const struct device *d,void (*cb)(const struct device *,void *))
{(void)d;(void)cb;return 0;}
static bool audio_is_recording(void){return false;}
static bool transfer_is_active(void){return false;}
static int storage_cleanup(void){return 0;}
static int storage_remount(void){remounts++;return 0;}
static int storage_ensure_mounted(void){remounts++;return 0;}
static void ble_notify_event(const char *a,const char *b){(void)a;(void)b;}
'''
        main = r'''
static void reset(void)
{
 usb_active=usb_vbus_present=initially_present=false;
 memset(&usb_timeout_work,0,sizeof(usb_timeout_work));registered=0;
 register_error=init_error=init_calls=enabled=disabled=remounts=0;
}
int main(void)
{
 reset();initially_present=true;
 assert(usb_cdc_init()==0 && usb_vbus_present);
 assert(usb_cdc_enable()==0 && usb_active && !usb_timeout_work.armed);
 usb_timeout_handler(0);assert(usb_active && disabled==0);
 struct usbd_msg removed={USBD_MSG_VBUS_REMOVED,cdc_dev};
 registered(&clip_usbd,&removed);
 assert(!usb_active && disabled==1 && remounts==1);

 reset();assert(usb_cdc_init()==0 && !usb_vbus_present);
 assert(usb_cdc_enable()==0 && usb_timeout_work.armed);
 assert(usb_timeout_work.delay==600000);
 usb_timeout_handler(0);assert(!usb_active && disabled==1);

 reset();assert(usb_cdc_init()==0);assert(usb_cdc_enable()==0);
 struct usbd_msg attached={USBD_MSG_VBUS_READY,cdc_dev};
 registered(&clip_usbd,&attached);
 assert(usb_vbus_present && !usb_timeout_work.armed);
 usb_timeout_handler(0);assert(usb_active && disabled==0);

 reset();register_error=-EIO;
 assert(usb_cdc_init()==-EIO && init_calls==0 && !usb_active);
 reset();init_error=-ENODEV;
 assert(usb_cdc_init()==-ENODEV && !usb_active);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Keep the compiled production linkage public, as in the real header.
            code = (fixture + '\n' + functions + '\n' + main).replace(
                'static int usb_cdc_disable(void);', 'int usb_cdc_disable(void);')
            (root / 'test.c').write_text(code)
            for msc in (0, 1):
                subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                                '-fsanitize=address,undefined', f'-DTEST_MSC={msc}',
                                str(root / 'test.c'), '-o', str(root / 'test')], check=True)
                subprocess.run([str(root / 'test')], check=True)
