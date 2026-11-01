// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Darren Chi
#include "battery.h"

#include "board.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "nvs.h"
#include "sense_adc.h"
#include "vbat_cal.h"

static const char *TAG = "battery";

#define VBAT_SETTLE_US  (BATTERY_SETTLE_MS * 1000LL)

// The measured curve lives in the factory partition (set up by vbat_cal_init()),
// so `cfg reset` and a merged image flashed at 0x0 leave it alone
#define CURVE_PART      "fctry"
#define CURVE_NAMESPACE "batt"
#define CURVE_KEY       "curve"
#define CURVE_MAGIC     0x31564342   // "BCV1"

typedef struct {
    uint32_t magic;
    batt_curve_t curve;
} curve_blob_t;

static batt_curve_t s_curve;
static bool s_curve_valid;

#define LOG_MAX         2048
#define LOG_MIN         20      // entries needed for a curve
static uint16_t s_log[LOG_MAX];
static int s_log_n;
static int s_log_step;          // readings per entry (doubles when the buffer fills)
static int s_acc_sum;
static int s_acc_n;
static int64_t s_log_start_us;

static void load_curve(void)
{
    nvs_handle_t h;
    if (nvs_open_from_partition(CURVE_PART, CURVE_NAMESPACE, NVS_READONLY, &h) != ESP_OK) {
        return;
    }
    curve_blob_t blob;
    size_t len = sizeof(blob);
    if (nvs_get_blob(h, CURVE_KEY, &blob, &len) == ESP_OK && len == sizeof(blob) && blob.magic == CURVE_MAGIC) {
        s_curve = blob.curve;
        s_curve_valid = true;
        ESP_LOGI(TAG, "measured battery curve: %u-%u mV over %lu min", s_curve.mv[0],
                 s_curve.mv[BATT_CURVE_POINTS - 1], (unsigned long)(s_curve.duration_s / 60));
    }
    nvs_close(h);
}

bool battery_curve(batt_curve_t *out)
{
    if (s_curve_valid && out) {
        *out = s_curve;
    }
    return s_curve_valid;
}

esp_err_t battery_set_curve(const batt_curve_t *curve)
{
    curve_blob_t blob = {.magic = CURVE_MAGIC, .curve = *curve};
    nvs_handle_t h;
    ESP_RETURN_ON_ERROR(nvs_open_from_partition(CURVE_PART, CURVE_NAMESPACE, NVS_READWRITE, &h), TAG, "open");
    esp_err_t err = nvs_set_blob(h, CURVE_KEY, &blob, sizeof(blob));
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);
    if (err == ESP_OK) {
        s_curve = *curve;
        s_curve_valid = true;
    }
    return err;
}

esp_err_t battery_clear_curve(void)
{
    nvs_handle_t h;
    ESP_RETURN_ON_ERROR(nvs_open_from_partition(CURVE_PART, CURVE_NAMESPACE, NVS_READWRITE, &h), TAG, "open");
    esp_err_t err = nvs_erase_key(h, CURVE_KEY);
    if (err == ESP_OK || err == ESP_ERR_NVS_NOT_FOUND) {
        err = nvs_commit(h);
    }
    nvs_close(h);
    if (err == ESP_OK) {
        s_curve_valid = false;
    }
    return err;
}

void battery_log_begin(void)
{
    s_log_n = 0;
    s_log_step = 1;
    s_acc_sum = 0;
    s_acc_n = 0;
    s_log_start_us = esp_timer_get_time();
}

void battery_log_add(int vbat_mv)
{
    s_acc_sum += vbat_mv;
    if (++s_acc_n < s_log_step) {
        return;
    }
    int mv = s_acc_sum / s_acc_n;
    s_acc_sum = 0;
    s_acc_n = 0;
    if (s_log_n == LOG_MAX) {
        for (int i = 0; i < LOG_MAX / 2; i++) {
            s_log[i] = (uint16_t)((s_log[2 * i] + s_log[2 * i + 1] + 1) / 2);
        }
        s_log_n = LOG_MAX / 2;
        s_log_step *= 2;
    }
    s_log[s_log_n++] = (uint16_t)mv;
}

uint32_t battery_log_elapsed_s(void)
{
    return (uint32_t)((esp_timer_get_time() - s_log_start_us) / 1000000);
}

bool battery_log_curve(batt_curve_t *out)
{
    int n = s_log_n;
    if (n < LOG_MIN) {
        return false;
    }
    // Each point averages the entries within 2.5 % of the run around its time,
    // narrowed to stay centred near the ends: one-sided, the 0 % point read high
    // (the voltage falls fastest just before the end)
    for (int k = 0; k < BATT_CURVE_POINTS; k++) {
        int c = ((n - 1) * k + (BATT_CURVE_POINTS - 1) / 2) / (BATT_CURVE_POINTS - 1);
        int win = n / 40;
        win = win < c ? win : c;
        win = win < n - 1 - c ? win : n - 1 - c;
        int lo = c - win;
        int hi = c + win;
        int sum = 0;
        for (int i = lo; i <= hi; i++) {
            sum += s_log[i];
        }
        out->mv[k] = (uint16_t)(sum / (hi - lo + 1));
    }
    // A battery's voltage only falls as it empties: smooth out any wobble upwards
    for (int k = 1; k < BATT_CURVE_POINTS; k++) {
        if (out->mv[k] > out->mv[k - 1]) {
            out->mv[k] = out->mv[k - 1];
        }
    }
    out->duration_s = battery_log_elapsed_s();
    return true;
}

esp_err_t battery_init(void)
{
    // Both inputs have external resistors: R2/R5 divider and R7 pull-up.
    gpio_config_t cfg = {
        .pin_bit_mask = BIT64(PIN_VUSB_DET) | BIT64(PIN_CHRG_DET),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    load_curve();
    return gpio_config(&cfg);
}

bool battery_usb_present(void)
{
    return gpio_get_level(PIN_VUSB_DET) == 1;
}

int battery_percent_from_mv(int mv)
{
    if (s_curve_valid) {
        const uint16_t *v = s_curve.mv;
        if (mv >= v[0]) {
            return 100;
        }
        for (int i = 1; i < BATT_CURVE_POINTS; i++) {
            if (mv >= v[i]) {
                int pct_hi = 100 - (i - 1) * BATT_CURVE_STEP_PCT;
                int span_mv = v[i - 1] - v[i];
                return span_mv > 0 ? pct_hi - (v[i - 1] - mv) * BATT_CURVE_STEP_PCT / span_mv : pct_hi;
            }
        }
        return 0;
    }

    // Rough LiPo discharge curve (HANDOVER.md 6.4)
    static const struct { int mv; int pct; } curve[] = {
        {3300, 0}, {3600, 10}, {3800, 50}, {4200, 100},
    };
    const int n = sizeof(curve) / sizeof(curve[0]);

    if (mv <= curve[0].mv) {
        return 0;
    }
    for (int i = 1; i < n; i++) {
        if (mv <= curve[i].mv) {
            int span_mv = curve[i].mv - curve[i - 1].mv;
            int span_pct = curve[i].pct - curve[i - 1].pct;
            return curve[i - 1].pct + (mv - curve[i - 1].mv) * span_pct / span_mv;
        }
    }
    return 100;
}

int battery_read_pin_mv(int samples)
{
    return sense_adc_read_mv(ADC_CH_VBAT, samples);
}

void battery_update(battery_status_t *out)
{
    bool usb = battery_usb_present();
    bool charging = gpio_get_level(PIN_CHRG_DET) == 0;   // needs R7 = 4.7k (board.h)

    out->src = !usb ? POWER_SRC_BATTERY : (charging ? POWER_SRC_CHARGING : POWER_SRC_FULL);

    if (esp_timer_get_time() < VBAT_SETTLE_US) {
        out->vbat_valid = false;
        return;
    }

    int pin_mv = battery_read_pin_mv(16);
    if (pin_mv < 0) {
        out->vbat_valid = false;
        return;
    }
    out->vbat_mv = vbat_cal_apply(pin_mv);
    // The charge current lifts the reading, so only trust it when no charging is
    // going on: on battery, or on USB once the TP4054 has finished (FULL)
    out->vbat_valid = out->src != POWER_SRC_CHARGING;
    out->percent = battery_percent_from_mv(out->vbat_mv);
}
