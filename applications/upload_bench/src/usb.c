/*
 * Copyright (c) 2025 Seeed Technology Co., Ltd.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/usb/usbd.h>

#include <zephyr/drivers/uart.h>

#include "usb_cdc.h"


#include "at_server.h"
#include "transport.h"


#include "clip_usb_dfu.h"

LOG_MODULE_REGISTER(usb, CONFIG_CLIP_LOG_LEVEL);

/* USB device definition (Seeed VID 0x2886) */
USBD_DEVICE_DEFINE(clip_usbd,
		   DEVICE_DT_GET(DT_NODELABEL(usbd)),
		   0x2886, 0x0069);

/* String descriptors */
USBD_DESC_LANG_DEFINE(clip_lang);
USBD_DESC_MANUFACTURER_DEFINE(clip_mfr, "Seeed Studio");
USBD_DESC_PRODUCT_DEFINE(clip_product, "Clip Upload Bench");

/* Configuration descriptor (bus-powered, 100mA) */
USBD_DESC_CONFIG_DEFINE(clip_fs_cfg, "Default");
USBD_CONFIGURATION_DEFINE(clip_fs_config, 0, 100, &clip_fs_cfg);

/* MSC LUN: SD card */

/* CDC ACM UART device */
static const struct device *const cdc_dev =
	DEVICE_DT_GET(DT_NODELABEL(cdc_acm_uart));

/* RX state */
static uint8_t rx_line_buf[CONFIG_CLIP_AT_MAX_CMD_LEN];
static uint16_t rx_line_pos;

static bool usb_active;
struct usbd_context *usb_cdc_get_usbd(void)
{
	return &clip_usbd;
}

bool usb_cdc_is_connected(void)
{
	return usb_active;
}

/* Submit accumulated line to AT server */
static void submit_rx_line(uint16_t len)
{
	if (len == 0) {
		return;
	}

	if (len > 0 && rx_line_buf[len - 1] == '\r') {
		len--;
	}

	if (len == 0) {
		return;
	}

	at_server_submit_cmd(rx_line_buf, len, TRANSPORT_TYPE_USB);
}

/* UART interrupt handler */
static void cdc_irq_handler(const struct device *dev, void *user_data)
{
	ARG_UNUSED(user_data);

	while (uart_irq_update(dev) && uart_irq_is_pending(dev)) {
		if (uart_irq_rx_ready(dev)) {
			uint8_t byte;
			int recv = uart_fifo_read(dev, &byte, 1);

			if (recv > 0) {
				if (byte == '\n') {
					submit_rx_line(rx_line_pos);
					rx_line_pos = 0;
				} else if (byte != '\r') {
					if (rx_line_pos < sizeof(rx_line_buf) - 1) {
						rx_line_buf[rx_line_pos++] = byte;
					}
				}
			}
		}
	}
}

int usb_cdc_send_response(const uint8_t *data, uint16_t len)
{
	if (!usb_active || len == 0) {
		return 0;
	}

	for (uint16_t i = 0; i < len; i++) {
		uart_poll_out(cdc_dev, data[i]);
	}

	return len;
}

static void usb_msg_cb(struct usbd_context *const ctx,const struct usbd_msg *msg)
{
    ARG_UNUSED(ctx);
    clip_usb_dfu_check(msg);
    if (msg->type==USBD_MSG_CDC_ACM_CONTROL_LINE_STATE) {
        uint32_t dtr=0;
        uart_line_ctrl_get(msg->dev,UART_LINE_CTRL_DTR,&dtr);
        if(dtr){uart_irq_rx_enable(cdc_dev);}else{uart_irq_rx_disable(cdc_dev);}
    }
}

int usb_cdc_init(void)
{
	int err;

	if (!device_is_ready(cdc_dev)) {
		LOG_ERR("CDC ACM device not ready");
		return -ENODEV;
	}

	/* Add USB descriptors */
	usbd_add_descriptor(&clip_usbd, &clip_lang);
	usbd_add_descriptor(&clip_usbd, &clip_mfr);
	usbd_add_descriptor(&clip_usbd, &clip_product);

	/* Add FS configuration */
	err = usbd_add_configuration(&clip_usbd, USBD_SPEED_FS, &clip_fs_config);
	if (err) {
		LOG_ERR("add config: %d", err);
		return err;
	}

	/* Register CDC ACM + MSC classes */
	err = usbd_register_class(&clip_usbd, "cdc_acm_0", USBD_SPEED_FS, 1);
	if (err) {
		LOG_ERR("CDC class: %d", err);
		return err;
	}

	/* Initialize USB device */
	err = usbd_init(&clip_usbd);
	if (err) {
		LOG_ERR("usbd init: %d", err);
		return err;
	}

	/* Register message callback */
	usbd_msg_register_cb(&clip_usbd, usb_msg_cb);

	/* Setup UART interrupt callback */
	uart_irq_callback_set(cdc_dev, cdc_irq_handler);

	return 0;
}

int usb_cdc_enable(void)
{
    if(usb_active){return 0;}
    int ret=usbd_enable(&clip_usbd);
    if(!ret){usb_active=true;}
    return ret;
}
bool usb_cdc_is_enabled(void){return usb_active;}
