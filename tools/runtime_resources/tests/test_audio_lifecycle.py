"""Exercise production audio lifecycle functions with host device/codec stubs."""
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]


def function(source, signature):
    start = source.index(signature + "\n{")
    brace = source.index("{", start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


class AudioLifecycleTests(unittest.TestCase):
    def test_stop_releases_state_and_same_parameters_can_restart(self):
        source = (ROOT / "applications/clip/src/audio.c").read_text()
        signatures = [
            "static void cleanup_opus_encoder(void)",
            "static void cleanup_speex_preprocessor(void)",
            "static bool encoder_params_changed(void)",
            "static bool dsp_params_changed(void)",
            "static int init_opus_encoder(void)",
            "static int init_speex_preprocessor(void)",
            "static int audio_stop_recording_internal(void)",
        ]
        bodies = "\n".join(function(source, s) for s in signatures)
        harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#define CONFIG_SPEEXDSP 1
#define LOG_INF(...) ((void)0)
#define LOG_ERR(...) ((void)0)
#define LOG_WRN(...) ((void)0)
#define CONFIG_CLIP_NORMAL_BITRATE 32000
#define CONFIG_CLIP_ENHANCED_BITRATE 32000
#define CONFIG_CLIP_NORMAL_COMPLEXITY 1
#define CONFIG_CLIP_ENHANCED_COMPLEXITY 1
#define CONFIG_CLIP_DEFAULT_NOISE -20
#define CONFIG_CLIP_DEFAULT_DEREVERB 0
#define AUDIO_SAMPLE_RATE 16000
#define AUDIO_OPUS_FRAME_SIZE 320
#define AUDIO_MODE_STEREO 1
#define OPUS_APPLICATION_AUDIO 2049
#define OPUS_SIGNAL_VOICE 3001
#define OPUS_OK 0
#define OPUS_SET_BITRATE(x) (x)
#define OPUS_SET_VBR(x) (x)
#define OPUS_SET_VBR_CONSTRAINT(x) (x)
#define OPUS_SET_COMPLEXITY(x) (x)
#define OPUS_SET_SIGNAL(x) (x)
#define OPUS_SET_LSB_DEPTH(x) (x)
#define OPUS_SET_DTX(x) (x)
#define OPUS_SET_INBAND_FEC(x) (x)
#define OPUS_SET_PACKET_LOSS_PERC(x) (x)
#define SPEEX_PREPROCESS_SET_NOISE_SUPPRESS 1
#define SPEEX_PREPROCESS_SET_DEREVERB 2
#define SPEEX_PREPROCESS_SET_DEREVERB_LEVEL 3
#define SPEEX_PREPROCESS_SET_DEREVERB_DECAY 4
#define IS_ENABLED(x) (x)
#define DMIC_TRIGGER_STOP 0
#define ENOMEM 12
typedef int opus_int32;
typedef struct { int value; } OpusEncoder;
typedef struct { int value; } SpeexPreprocessState;
static OpusEncoder *opus_encoder;
static SpeexPreprocessState *speex_pp;
static bool speex_enabled, recording_active, is_paused;
static struct { uint32_t bitrate; uint8_t complexity; int channels; bool initialized; } encoder_params;
static struct { uint8_t noise_suppress; bool dereverb_enabled, initialized; } dsp_params;
static struct clip_context { struct { uint8_t noise_suppress; bool dereverb_enabled; } config; } context;
static struct { bool is_open; } current_storage_file;
static struct { unsigned frames_encoded, encode_time_avg_us, recording_time_ms; } stats;
static uint64_t encode_time_total_us;
static int current_mode, opus_channels, current_file_index;
static const void *dmic_dev;
static char current_session_id[]="20261019000000";
static int enc_live, dsp_live, creates, closes, session_closes, power_offs, releases;
static bool fail_io;
static struct clip_context *clip_get_context(void) { return &context; }
static OpusEncoder *opus_encoder_create(int rate,int channels,int app,int *err) {
    (void)rate;(void)channels;(void)app;*err=0;enc_live++;creates++;
    return calloc(1,sizeof(OpusEncoder));
}
static int opus_encoder_ctl(OpusEncoder *p,int request,...) {
    (void)request;assert(p && enc_live==1);return 0;
}
static void opus_encoder_destroy(OpusEncoder *p) { assert(enc_live==1);enc_live--;free(p); }
static SpeexPreprocessState *speex_preprocess_state_init(int frames,int rate) {
    (void)frames;(void)rate;dsp_live++;return calloc(1,sizeof(SpeexPreprocessState));
}
static int speex_preprocess_ctl(SpeexPreprocessState *p,int request,void *value) {
    (void)request;(void)value;assert(p && dsp_live==1);return 0;
}
static void speex_preprocess_state_destroy(SpeexPreprocessState *p) { assert(dsp_live==1);dsp_live--;free(p); }
static int dmic_trigger(const void *dev,int cmd) { (void)dev;(void)cmd;return fail_io?-1:0; }
static int storage_close_file(void *f) { (void)f;closes++;current_storage_file.is_open=false;return fail_io?-1:0; }
static int storage_close_session(const char *s,unsigned d,int file) {
    (void)s;(void)d;(void)file;session_closes++;return fail_io?-1:0;
}
static void config_sync_time(void) { assert(!current_storage_file.is_open); }
static int mic_power_off(void) { power_offs++;return 0; }
static void clip_cpu_boost_release(void) {
    assert(!opus_encoder && !speex_pp && !enc_live && !dsp_live);releases++;
}
'''
        cases = r'''
int main(void) {
    context.config.noise_suppress=20;
    /* Normal, enhanced, same-mode restart and stop-while-paused. */
    const int modes[]={1,0,0,1};
    for(unsigned i=0;i<4;i++) {
        current_mode=modes[i];opus_channels=current_mode==1?2:1;
        assert(init_opus_encoder()==0);
        if(current_mode!=1) { assert(init_speex_preprocessor()==0);speex_enabled=true; }
        assert(enc_live==1);
        recording_active=true;is_paused=(i==2);current_storage_file.is_open=!is_paused;
        fail_io=(i==3); /* SD/DMIC failures must not retain codec allocations. */
        assert(audio_stop_recording_internal()==0);
        assert(!recording_active && !is_paused && !speex_enabled);
        assert(!opus_encoder && !speex_pp && !enc_live && !dsp_live);
        assert(!encoder_params.initialized && !dsp_params.initialized);
        assert(creates==(int)i+1);
        assert(audio_stop_recording_internal()==0); /* no double destruction */
        assert(releases==(int)i+1);
    }
    assert(closes==3 && session_closes==4 && power_offs==4);
    cleanup_opus_encoder();cleanup_speex_preprocessor();
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            (temp / "test.c").write_text(harness + bodies + cases)
            subprocess.run([
                "clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                "-Wno-unused-variable", "-fsanitize=address,undefined",
                str(temp / "test.c"), "-o", str(temp / "test"),
            ], check=True)
            subprocess.run([str(temp / "test")], check=True)


if __name__ == "__main__":
    unittest.main()
