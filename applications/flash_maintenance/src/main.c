/* SPDX-License-Identifier: Apache-2.0 */
#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/flash.h>
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/sys/crc.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>

#define FLASH_BYTES (DT_PROP(DT_NODELABEL(spi_flash), size) / 8U)
#define WRITE_START 0x130000U
#define ERASE_BYTES 0x10000U
#define CHUNK_BYTES 0x4000U

static const struct device *const nor = DEVICE_DT_GET(DT_NODELABEL(spi_flash));
static const struct device *const cdc = DEVICE_DT_GET(DT_NODELABEL(cdc_acm_uart));
static uint8_t buffer[CHUNK_BYTES];
static bool armed;

static void send_bytes(const void *data, size_t size)
{
    const uint8_t *p = data;
    for (size_t i = 0; i < size; i++) uart_poll_out(cdc, p[i]);
}

static void send_error(int code)
{
    char line[32];
    int n = snprintf(line, sizeof(line), "ERR %d\n", code);
    send_bytes(line, n);
}

static bool valid_range(unsigned int offset, unsigned int size, bool write)
{
    return size && offset < FLASH_BYTES && size <= FLASH_BYTES - offset &&
           (!write || (armed && offset >= WRITE_START));
}

static bool receive_payload(unsigned int size)
{
    unsigned int got = 0;
    int64_t deadline = k_uptime_get() + 10000;
    while (got < size) {
        if (uart_poll_in(cdc, &buffer[got]) == 0) got++;
        else {
            if (k_uptime_get() >= deadline) return false;
            k_sleep(K_MSEC(1));
        }
    }
    return true;
}

/* Returns false only when a truncated binary request makes framing unsafe.
 * Recovery remains available through the shared 1200-baud USB trigger.
 */
static bool command(char *line)
{
    if (!strcmp(line, "ARM LFS_PATCH_0x130000_0x800000")) {
        armed = true;
        send_bytes("OK ARM\n", 7);
        return true;
    }
    if (!strcmp(line, "INFO")) {
        uint8_t id[8];
        int n = hwinfo_get_device_id(id, sizeof(id));
        if (n != (int)sizeof(id)) { send_error(-ENODEV); return true; }
        char response[160];
        n = snprintf(response, sizeof(response),
            "INFO 1 %x %x %x %x %02x%02x%02x%02x%02x%02x%02x%02x\n",
            (unsigned int)FLASH_BYTES, WRITE_START, ERASE_BYTES, CHUNK_BYTES,
            id[0], id[1], id[2], id[3], id[4], id[5], id[6], id[7]);
        send_bytes(response, n);
        return true;
    }
    char op[12], extra;
    unsigned int offset = 0, size = 0, expected_crc = 0;
    int fields = sscanf(line, "%11s %x %x %x %c", op, &offset, &size, &expected_crc, &extra);
    int ret;
    if (fields == 3 && !strcmp(op, "READ") && size <= sizeof(buffer) &&
        valid_range(offset, size, false)) {
        ret = flash_read(nor, offset, buffer, size);
        if (ret) { send_error(ret); return true; }
        char header[64];
        int n = snprintf(header, sizeof(header), "DATA %x %08x\n", size,
                         (unsigned int)crc32_ieee(buffer, size));
        send_bytes(header, n);
        send_bytes(buffer, size);
        return true;
    }
    if (fields == 3 && !strcmp(op, "ERASE") &&
        valid_range(offset, size, true) && size == ERASE_BYTES && !(offset % ERASE_BYTES)) {
        ret = flash_erase(nor, offset, size);
        if (ret) send_error(ret); else send_bytes("OK ERASE\n", 9);
        return true;
    }
    if (fields == 4 && !strcmp(op, "WRITE") && size <= sizeof(buffer) &&
        valid_range(offset, size, true)) {
        send_bytes("READY\n", 6);
        if (!receive_payload(size)) {
            armed = false;
            send_error(-ETIMEDOUT);
            return false;
        }
        if (crc32_ieee(buffer, size) != expected_crc) { send_error(-EBADMSG); return true; }
        ret = flash_write(nor, offset, buffer, size);
        if (ret) { send_error(ret); return true; }
        ret = flash_read(nor, offset, buffer, size);
        if (ret || crc32_ieee(buffer, size) != expected_crc) send_error(ret ? ret : -EIO);
        else send_bytes("OK WRITE\n", 9);
        return true;
    }
    send_error(-EINVAL);
    return true;
}

int main(void)
{
    if (!device_is_ready(nor) || !device_is_ready(cdc)) return -ENODEV;
    struct flash_pages_info page;
    if (flash_get_page_info_by_offs(nor, WRITE_START, &page) || page.size != ERASE_BYTES)
        return -EINVAL;
    char line[128];
    size_t used = 0;
    bool overflow = false;
    while (true) {
        uint8_t c;
        if (uart_poll_in(cdc, &c)) { k_sleep(K_MSEC(1)); continue; }
        if (c == '\r') continue;
        if (c == '\n') {
            line[used] = '\0';
            if (overflow) send_error(-EMSGSIZE);
            else if (!command(line)) {
                while (true) k_sleep(K_SECONDS(1));
            }
            used = 0;
            overflow = false;
        } else if (c < 32 || c > 126 || used >= sizeof(line)-1) overflow = true;
        else line[used++] = c;
    }
}
