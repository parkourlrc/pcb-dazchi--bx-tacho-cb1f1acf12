// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Darren Chi
// Battery voltage, USB presence and charge state (HANDOVER.md 6.4).
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

typedef enum {
    POWER_SRC_BATTERY,    // VUSB_DET LOW
    POWER_SRC_CHARGING,   // VUSB_DET HIGH, CHRG_DET LOW
    POWER_SRC_FULL,       // VUSB_DET HIGH, CHRG_DET HIGH
} power_src_t;

typedef struct {
    power_src_t src;
    int vbat_mv;          // last reading; raised by the charge current while charging
    int percent;          // 0-100, only meaningful when vbat_valid
    bool vbat_valid;      // false while charging or before the first good reading
} battery_status_t;

// The R3/R9 divider with C5 has a ~24 ms time constant; VBAT is not read
// until this long after boot (vbat_mv stays 0 until the first reading).
#define BATTERY_SETTLE_MS 150

esp_err_t battery_init(void);
bool battery_usb_present(void);
void battery_update(battery_status_t *out);
// VBAT_SENSE at the ADC pin (VBAT / 2 nominally), averaged; -1 on error
int battery_read_pin_mv(int samples);
int battery_percent_from_mv(int mv);

// Battery percentage curve measured by a full discharge at a steady load
// (cal batt): VBAT at 100, 90, ... 0 %. Kept in the factory partition beside
// the VBAT calibration. Without one a rough LiPo curve is used.
#define BATT_CURVE_POINTS   11
#define BATT_CURVE_STEP_PCT 10
typedef struct {
    uint16_t mv[BATT_CURVE_POINTS];   // [0] at 100 %, [BATT_CURVE_POINTS - 1] at 0 %
    uint32_t duration_s;              // the discharge it came from, full to the end voltage
} batt_curve_t;
bool battery_curve(batt_curve_t *out);   // false: none measured, the rough curve is in use
esp_err_t battery_set_curve(const batt_curve_t *curve);
esp_err_t battery_clear_curve(void);

// The discharge log behind it: one VBAT reading per period, in RAM. When the
// buffer fills, neighbouring entries are merged and the period doubles, so a
// run of any length fits.
void battery_log_begin(void);
void battery_log_add(int vbat_mv);
uint32_t battery_log_elapsed_s(void);
// With a constant current the charge left falls in step with time, so the
// voltage at a fraction of the run gives that percentage. False if too few
// entries.
bool battery_log_curve(batt_curve_t *out);
