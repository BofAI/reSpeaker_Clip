"""Compile the actual upload event gate and dispatcher, without hardware."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PreemptionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        source = (ROOT / "applications/clip/src/https_poc.c").read_text()
        event_source = (ROOT / "applications/clip/src/clip_event.c").read_text()
        events = (ROOT / "applications/clip/include/clip_event.h").read_text()
        enums = events[events.index("enum clip_event {"):events.index("/**\n * @brief Event handler result info")]
        cancel = "static void request_cancel" + source.split("static void request_cancel", 1)[1].split(
            "static void stall_handler", 1)[0]
        getters = "bool https_poc_active" + source.split("bool https_poc_active", 1)[1].split(
            "bool https_poc_command_allowed", 1)[0]
        gate = "bool https_poc_event" + source.split("bool https_poc_event", 1)[1].split(
            "static int check_cancel", 1)[0]
        valid = "static bool valid_session" + source.split("static bool valid_session", 1)[1].split(
            "static int hash_file", 1)[0]
        start = "static int start_upload" + source.split("static int start_upload", 1)[1].split(
            "static int command", 1)[0]
        dispatch = "void clip_event_process" + event_source.split("void clip_event_process", 1)[1].split(
            "static enum clip_event_result execute_transition", 1)[0]
        code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <ctype.h>
#include <errno.h>
#define CONFIG_CLIP_HTTPS_POC 1
#define CONFIG_USBD_MSC_CLASS 0
#define IS_ENABLED(x) (x)
#define ARG_UNUSED(x) (void)(x)
#define LOG_WRN(...) ((void)0)
#define LOG_INF(...) ((void)0)
#define K_NO_WAIT 0
#define TRANS_INVALID 255
#define TRANS_SAME 254
typedef int atomic_val_t;
enum { UI_EVENT_WIFI_BLOCKED, UI_EVENT_USB_BLOCKED };
static void display_post_event(int event) { (void)event; }
enum clip_state { CLIP_STATE_IDLE, CLIP_STATE_RECORDING, CLIP_STATE_WIFI_SYNC };
'''
        code += enums
        code += r'''
struct clip_event_result_info { enum clip_event_result result; int error_code; };
struct clip_event_item { enum clip_event event; struct clip_event_result_info *result; int *done_sem; };
static int active, cancelled, stalled, last_error, bytes_sent, stage, record_pending, record_error, cleanup_error;
static int confirmed, resume_from, total_bytes;
static int start_job, g_state, clip_ev_msgq, ota_in_progress;
static bool audio_active, resources_released, inject_start;
static int interrupts, record_starts, started_jobs;
static enum clip_event_result recording_result = CLIP_EVENT_OK;
static char ssid[33] = "fixture", host[96] = "receiver", session[32];
static unsigned int segment;
static bool time_ready = true;
enum { PREPARING = 1 };
static struct clip_event_item queue[4];
static unsigned int queued, consumed;
static uint8_t transition_table[3][CLIP_EVENT_COUNT];
static int atomic_get(int *v) { return *v; }
static void atomic_set(int *v, int n) { *v = n; }
static void atomic_clear(int *v) { *v = 0; }
static bool atomic_cas(int *v, int old, int n) { if (*v != old) return false; *v = n; return true; }
static void k_sem_give(int *sem) { (*sem)++; }
static bool audio_is_recording(void) { return audio_active; }
static bool transfer_is_active(void) { return false; }
static bool usb_cdc_is_enabled(void) { return false; }
static void wifi_diag_reset(void) { started_jobs++; }
static int k_msgq_get(int *msgq, struct clip_event_item *out, int wait) {
    (void)msgq; (void)wait;
    if (consumed == queued) return -EAGAIN;
    *out = queue[consumed++]; return 0;
}
static int clip_post_event_sync(enum clip_event event, struct clip_event_result_info *info);
static enum clip_event_result execute_transition(enum clip_event event,
                                                 enum clip_state from, enum clip_state to) {
    assert(event == CLIP_EVENT_START && from == CLIP_STATE_IDLE && to == CLIP_STATE_RECORDING);
    assert(resources_released && !active);
    record_starts++;
    if (recording_result == CLIP_EVENT_OK) audio_active = true;
    return recording_result;
}
'''
        code += cancel + getters + gate + valid + start + dispatch
        code += r'''
static int clip_post_event_sync(enum clip_event event, struct clip_event_result_info *info) {
    assert(https_poc_event(event, info, g_state == CLIP_STATE_IDLE));
    if (inject_start && event == CLIP_EVENT_UPLOAD_BEGIN) {
        struct clip_event_result_info recording;
        assert(https_poc_event(CLIP_EVENT_START, &recording, true));
        assert(recording.result == CLIP_EVENT_OK && recording.error_code == -EINPROGRESS);
    }
    return 0;
}
static void finish_upload(void) {
    int done = 0;
    struct clip_event_result_info result;
    resources_released = true;
    queue[queued++] = (struct clip_event_item){CLIP_EVENT_UPLOAD_END, &result, &done};
    clip_event_process();
    assert(done == 1);
}
int main(int argc, char **argv) {
    assert(argc == 2);
    memset(transition_table, TRANS_INVALID, sizeof(transition_table));
    transition_table[CLIP_STATE_IDLE][CLIP_EVENT_START] = CLIP_STATE_RECORDING;
    struct clip_event_result_info info;
    char selector[] = "20261019070446:1";
    inject_start = !strcmp(argv[1], "startup-race");
    cancelled = stalled = last_error = bytes_sent = 1;
    assert(start_upload(selector) == 0);
    assert(started_jobs == 1 && start_job == 1);
    assert(!stalled && !last_error && !bytes_sent);
    assert(!strcmp(session, "20261019070446") && segment == 1);
    if (!inject_start) {
        assert(!cancelled);
        assert(https_poc_event(CLIP_EVENT_START, &info, true));
        assert(info.result == CLIP_EVENT_OK && info.error_code == -EINPROGRESS);
    }
    assert(active && cancelled && record_pending && !interrupts && !record_starts);
    assert(https_poc_event(CLIP_EVENT_START, &info, true));
    assert(https_poc_event(CLIP_EVENT_UPLOAD_BEGIN, &info, true));
    assert(info.result == CLIP_EVENT_BUSY && cancelled && record_pending);
    if (!strcmp(argv[1], "stop")) {
        assert(https_poc_event(CLIP_EVENT_STOP, &info, true));
        assert(info.result == CLIP_EVENT_OK && !record_pending && record_error == -ECANCELED);
    } else if (!strcmp(argv[1], "poweroff")) {
        assert(https_poc_event(CLIP_EVENT_POWER_OFF_EXEC, &info, true));
        assert(!record_pending && record_error == -ECANCELED);
    } else if (!strcmp(argv[1], "failure")) {
        recording_result = CLIP_EVENT_ERROR;
    } else if (!strcmp(argv[1], "cleanup-failure")) {
        cleanup_error = -EIO;
    } else if (!strcmp(argv[1], "full-queue")) {
        /* Invalid queued events fill all remaining slots. END must replay
         * START in place, without trying to enqueue another event. */
        for (int i=0; i<3; i++) queue[queued++] = (struct clip_event_item){CLIP_EVENT_COUNT, NULL, NULL};
    }
    assert(cancelled && !interrupts);
    finish_upload();
    assert(!active && !record_pending);
    if (!strcmp(argv[1], "cleanup-failure")) {
        assert(!record_starts && !audio_active && record_error == -EIO);
    } else if (!strcmp(argv[1], "stop") || !strcmp(argv[1], "poweroff")) {
        assert(!record_starts && !audio_active);
    } else {
        assert(record_starts == 1);
        assert(record_error == (recording_result == CLIP_EVENT_OK ? 0 : -EIO));
        assert(audio_active == (recording_result == CLIP_EVENT_OK));
        assert(g_state == (recording_result == CLIP_EVENT_OK ? CLIP_STATE_RECORDING : CLIP_STATE_IDLE));
    }
    cancelled = 0;
    /* Cancellation creates no background work capable of touching a new job. */
    assert(!interrupts);
    return 0;
}
'''
        (root / "test.c").write_text(code)
        cls.executable = root / "test"
        try:
            subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", str(root / "test.c"),
                            "-o", str(cls.executable)], check=True)
        except BaseException:
            cls.directory.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_record_request_before_worker_start_survives(self):
        subprocess.run([str(self.executable), "startup-race"], check=True)

    def test_cleanup_replays_once_even_when_queue_full(self):
        subprocess.run([str(self.executable), "full-queue"], check=True)

    def test_stop_cancels_pending_recording(self):
        subprocess.run([str(self.executable), "stop"], check=True)

    def test_poweroff_cancels_pending_recording(self):
        subprocess.run([str(self.executable), "poweroff"], check=True)

    def test_record_start_failure_is_visible(self):
        subprocess.run([str(self.executable), "failure"], check=True)

    def test_failed_radio_cleanup_does_not_start_recording(self):
        subprocess.run([str(self.executable), "cleanup-failure"], check=True)


if __name__ == "__main__":
    unittest.main()
