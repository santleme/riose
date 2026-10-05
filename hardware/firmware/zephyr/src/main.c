#include <errno.h>
#include <stdint.h>

#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/drivers/i2c.h>
#include <zephyr/drivers/spi.h>
#include <zephyr/drivers/watchdog.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

#include "tag_firmware.h"
#include "tag_reset_cause.h"

LOG_MODULE_REGISTER(cattle_tag, LOG_LEVEL_INF);

K_SEM_DEFINE(tag_event_sem, 0, 1);
#define TAG_WATCHDOG_TIMEOUT_MS 10000u
#define TAG_WATCHDOG_FEED_SLICE_MS 5000u
#define TAG_RESET_FLAGS_KNOWN_MASK ((1u << 9) - 1u)
static struct gpio_callback imu_gpio_cb;
static struct gpio_callback radio_gpio_cb;
static bool reset_cause_available;
static uint32_t previous_reset_cause;
static tag_reset_kind_t previous_reset_kind;

#if defined(CONFIG_WATCHDOG)
static const struct device *const tag_watchdog = DEVICE_DT_GET(DT_NODELABEL(iwdg));
static int tag_watchdog_channel = -1;

static int watchdog_start(void)
{
    if (!device_is_ready(tag_watchdog)) return -ENODEV;
    const struct wdt_timeout_cfg timeout = {
        .window = {.min = 0u, .max = TAG_WATCHDOG_TIMEOUT_MS},
        .callback = NULL,
    };
    tag_watchdog_channel = wdt_install_timeout(tag_watchdog, &timeout);
    if (tag_watchdog_channel < 0) return tag_watchdog_channel;
    const int rc = wdt_setup(tag_watchdog, 0u);
    if (rc != 0) {
        tag_watchdog_channel = -1;
        return rc;
    }
    LOG_INF("MCU watchdog active, timeout_ms=%u", TAG_WATCHDOG_TIMEOUT_MS);
    return 0;
}

static int watchdog_feed(void)
{
    return tag_watchdog_channel < 0 ? -ENODEV
        : wdt_feed(tag_watchdog, tag_watchdog_channel);
}
#else
static int watchdog_start(void) { return 0; }
static int watchdog_feed(void) { return 0; }
#endif

static void report_and_clear_reset_cause(void)
{
#if defined(CONFIG_HWINFO)
    uint32_t cause = 0u;
    const int rc = hwinfo_get_reset_cause(&cause);
    if (rc != 0) {
        LOG_WRN("MCU_RESET_CAUSE unavailable (%d)", rc);
        return;
    }
    const tag_reset_kind_t kind = tag_reset_cause_classify(cause);
    reset_cause_available = cause != 0u;
    previous_reset_cause = cause;
    previous_reset_kind = kind;
    LOG_INF("MCU_RESET_CAUSE,flags=0x%08x,kind=%s,unknown_bits=0x%08x",
            cause, tag_reset_kind_name(kind), cause & ~TAG_RESET_FLAGS_KNOWN_MASK);
    const int clear_rc = hwinfo_clear_reset_cause();
    if (clear_rc != 0) LOG_WRN("Could not clear reset-cause flags (%d)", clear_rc);
#else
    LOG_INF("MCU_RESET_CAUSE unavailable in this backend");
#endif
}

#if !DT_NODE_HAS_STATUS(DT_ALIAS(tag_radio), okay) || \
    !DT_NODE_HAS_STATUS(DT_ALIAS(tag_imu), okay) || \
    !DT_NODE_HAS_STATUS(DT_ALIAS(tag_radio_busy), okay) || \
    !DT_NODE_HAS_STATUS(DT_ALIAS(tag_state_trace), okay)
#error "Add tag-radio, tag-imu, tag-radio-busy, and tag-state-trace aliases"
#endif

static const struct spi_dt_spec radio_spi = SPI_DT_SPEC_GET(
    DT_ALIAS(tag_radio), SPI_OP_MODE_MASTER | SPI_WORD_SET(8) |
                         SPI_TRANSFER_MSB, 0);
static const struct i2c_dt_spec imu_i2c = I2C_DT_SPEC_GET(DT_ALIAS(tag_imu));
static const struct gpio_dt_spec radio_reset =
    GPIO_DT_SPEC_GET(DT_ALIAS(tag_radio_reset), gpios);
static const struct gpio_dt_spec radio_busy =
    GPIO_DT_SPEC_GET(DT_ALIAS(tag_radio_busy), gpios);
static const struct gpio_dt_spec radio_dio1 =
    GPIO_DT_SPEC_GET(DT_ALIAS(tag_radio_dio1), gpios);
static const struct gpio_dt_spec radio_ant_switch =
    GPIO_DT_SPEC_GET(DT_ALIAS(tag_radio_ant_switch), gpios);
static const struct gpio_dt_spec imu_int =
    GPIO_DT_SPEC_GET(DT_ALIAS(tag_imu_int), gpios);
static const struct gpio_dt_spec state_trace_pins[] = {
    GPIO_DT_SPEC_GET_BY_IDX(DT_ALIAS(tag_state_trace), gpios, 0),
    GPIO_DT_SPEC_GET_BY_IDX(DT_ALIAS(tag_state_trace), gpios, 1),
    GPIO_DT_SPEC_GET_BY_IDX(DT_ALIAS(tag_state_trace), gpios, 2),
};

static uint32_t structured_trace_sequence;

static int spi_transfer(void *context, const uint8_t *tx, size_t tx_len,
                        uint8_t *rx, size_t rx_len)
{
    ARG_UNUSED(context);
    if (tx == NULL || rx == NULL || tx_len == 0 || tx_len != rx_len) {
        return -EINVAL;
    }

    struct spi_buf tx_buf = {.buf = (void *)tx, .len = tx_len};
    struct spi_buf rx_buf = {.buf = rx, .len = rx_len};
    const struct spi_buf_set tx_set = {.buffers = &tx_buf, .count = 1};
    const struct spi_buf_set rx_set = {.buffers = &rx_buf, .count = 1};
    return spi_transceive_dt(&radio_spi, &tx_set, &rx_set);
}

static int radio_busy_fn(void *context)
{
    ARG_UNUSED(context);
    return gpio_pin_get_dt(&radio_busy);
}

static int radio_reset_fn(void *context)
{
    ARG_UNUSED(context);
    int rc = gpio_pin_set_dt(&radio_reset, 1);
    if (rc != 0) return rc;
    k_msleep(2);
    rc = gpio_pin_set_dt(&radio_reset, 0);
    if (rc != 0) return rc;
    k_msleep(5);
    return 0;
}

static int imu_write_register(uint8_t reg, uint8_t value)
{
    const uint8_t bytes[] = {reg, value};
    return i2c_write_dt(&imu_i2c, bytes, sizeof(bytes));
}

static int imu_configure(void)
{
    uint8_t reg = 0x0f; /* WHO_AM_I: LIS2DW12 returns 0x44. */
    uint8_t identity = 0;
    int rc = i2c_write_read_dt(&imu_i2c, &reg, sizeof(reg), &identity,
                               sizeof(identity));
    if (rc != 0) return rc;
    if (identity != 0x44) return -ENODEV;
    /* BDU + IF_ADD_INC: the sample read below starts at OUT_X_L and fetches
     * all six axis bytes in one I2C transaction. */
    rc = imu_write_register(0x21, 0x0C);
    if (rc != 0) return rc;
    rc = imu_write_register(0x20, 0x14); /* CTRL1: 12.5 Hz, high-performance 14-bit mode */
    if (rc != 0) return rc;
    rc = imu_write_register(0x34, 0x02); /* WAKE_UP_THS: 62.5 mg at +/-2 g */
    if (rc != 0) return rc;
    rc = imu_write_register(0x35, 0x00); /* WAKE_UP_DUR: no extra debounce */
    if (rc != 0) return rc;

    /* Source registers are read-to-clear; discard any power-up/latching event
     * before enabling the route and global interrupt gate. */
    uint8_t source_reg = 0x38; /* WAKE_UP_SRC */
    uint8_t source = 0;
    rc = i2c_write_read_dt(&imu_i2c, &source_reg, sizeof(source_reg),
                           &source, sizeof(source));
    if (rc != 0) return rc;
    source_reg = 0x3b; /* ALL_INT_SRC */
    rc = i2c_write_read_dt(&imu_i2c, &source_reg, sizeof(source_reg),
                           &source, sizeof(source));
    if (rc != 0) return rc;
    rc = imu_write_register(0x23, 0x20); /* CTRL4.INT1_WU (bit 5) -> INT1 */
    if (rc != 0) return rc;
    return imu_write_register(0x3f, 0x20); /* CTRL7.INTERRUPTS_ENABLE */
}

static int imu_read_fn(void *context, tag_imu_sample_t *sample)
{
    ARG_UNUSED(context);
    if (sample == NULL) return -EINVAL;
    uint8_t reg = 0xa8; /* OUT_X_L | auto increment */
    uint8_t data[6] = {0};
    int rc = i2c_write_read_dt(&imu_i2c, &reg, sizeof(reg), data, sizeof(data));
    if (rc != 0) return rc;

    for (size_t axis = 0; axis < 3; ++axis) {
        const int16_t raw16 = (int16_t)((uint16_t)data[axis * 2] |
                               ((uint16_t)data[axis * 2 + 1] << 8));
        /* CTRL1 selects high-performance 14-bit output, left-aligned in OUT_x.
         * At +/-2 g the LIS2DW12 sensitivity is 0.244 mg/LSB. */
        const int32_t raw14 = raw16 >> 2;
        const int16_t mg = (int16_t)((raw14 * 244) / 1000);
        if (axis == 0) sample->x_mg = mg;
        else if (axis == 1) sample->y_mg = mg;
        else sample->z_mg = mg;
    }
    uint8_t source_reg = 0x38; /* Reading WAKE_UP_SRC clears the latched WU event. */
    uint8_t wake_source = 0;
    rc = i2c_write_read_dt(&imu_i2c, &source_reg, sizeof(source_reg),
                           &wake_source, sizeof(wake_source));
    if (rc != 0) return rc;
    sample->interrupt_flags = (wake_source & 0x08u) != 0u
        ? TAG_IMU_FLAG_WAKE_UP : 0u;
    return 0;
}

static bool imu_irq_pending(void *context)
{
    ARG_UNUSED(context);
    return gpio_pin_get_dt(&imu_int) > 0;
}

static bool radio_irq_pending(void *context)
{
    ARG_UNUSED(context);
    return gpio_pin_get_dt(&radio_dio1) > 0;
}

static uint32_t clock_ms(void *context)
{
    ARG_UNUSED(context);
    return k_uptime_get_32();
}

static uint64_t clock_us(void *context)
{
    ARG_UNUSED(context);
    return (uint64_t)k_uptime_get() * 1000u;
}

static void sleep_ms(void *context, uint32_t duration_ms)
{
    ARG_UNUSED(context);
    k_sleep(K_MSEC(duration_ms));
}

static void tag_gpio_isr(const struct device *port, struct gpio_callback *cb,
                         gpio_port_pins_t pins)
{
    ARG_UNUSED(port);
    ARG_UNUSED(cb);
    ARG_UNUSED(pins);
    k_sem_give(&tag_event_sem);
}

static void wait_for_event(void *context, uint32_t timeout_ms)
{
    ARG_UNUSED(context);
    /* Level check closes the gap between the FSM's IRQ check and taking the
     * semaphore. INT1/DIO1 callbacks then wake the MCU without periodic polls. */
    if (gpio_pin_get_dt(&imu_int) > 0 || gpio_pin_get_dt(&radio_dio1) > 0) return;
    const int64_t deadline = k_uptime_get() + timeout_ms;
    while (true) {
        const int64_t remaining = deadline - k_uptime_get();
        if (remaining <= 0) return;
        const uint32_t slice = (uint32_t)MIN(remaining, TAG_WATCHDOG_FEED_SLICE_MS);
        if (k_sem_take(&tag_event_sem, K_MSEC(slice)) == 0) return;
        const int rc = watchdog_feed();
        if (rc != 0) {
            LOG_ERR("Watchdog feed failed while waiting for event (%d)", rc);
            return;
        }
    }
}

static void state_trace(void *context, tag_state_t state)
{
    ARG_UNUSED(context);
    const uint8_t code = (uint8_t)state;
    for (size_t bit = 0; bit < ARRAY_SIZE(state_trace_pins); ++bit) {
        (void)gpio_pin_set_dt(&state_trace_pins[bit], (code >> bit) & 1u);
    }
}

static const char *trace_event_name(tag_trace_event_t event)
{
    static const char *const names[] = {
        "INVALID", "BOOT", "MCU_INIT", "STATE", "IMU_READ", "PACKET_CREATED",
        "RADIO_STANDBY", "TX_START", "TX_DONE", "RX_START", "RX_DONE",
        "RADIO_SLEEP", "ERROR", "RECOVERY", "MCU_SLEEP", "WAKE", "SPI",
        "IRQ", "TIMEOUT", "WATCHDOG", "REBOOT", "TRACE_END"
    };
    return (unsigned)event < ARRAY_SIZE(names) ? names[event] : "UNKNOWN";
}

static void trace_event(void *context, const tag_trace_record_t *record)
{
    ARG_UNUSED(context);
    LOG_INF("SIMULATED_TRACE,v1,%u,%llu,%u,%s,%u,%u,%d,%u,%u,%u,%s",
            structured_trace_sequence++,
            (unsigned long long)record->timestamp_us,
            (unsigned)record->state, trace_event_name(record->event),
            (unsigned)record->event, (unsigned)record->source, (int)record->result,
            record->value0, record->value1, record->value2, record->packet_hex);
}

int main(void)
{
    report_and_clear_reset_cause();
    int rc = watchdog_start();
    if (rc != 0) {
        LOG_ERR("MCU watchdog initialization failed (%d)", rc);
        return rc;
    }
    if (!spi_is_ready_dt(&radio_spi) || !i2c_is_ready_dt(&imu_i2c) ||
        !gpio_is_ready_dt(&radio_reset) || !gpio_is_ready_dt(&radio_busy) ||
        !gpio_is_ready_dt(&radio_dio1) || !gpio_is_ready_dt(&radio_ant_switch) ||
        !gpio_is_ready_dt(&imu_int)) {
        LOG_ERR("A required SPI, I2C, or GPIO device is not ready");
        return -ENODEV;
    }
    rc = gpio_pin_configure_dt(&radio_reset, GPIO_OUTPUT_INACTIVE);
    if (rc != 0) return rc;
    rc = gpio_pin_configure_dt(&radio_busy, GPIO_INPUT);
    if (rc != 0) return rc;
    rc = gpio_pin_configure_dt(&radio_dio1, GPIO_INPUT);
    if (rc != 0) return rc;
    /* Shield ANT_SW is held high; SX1262 DIO2 selects TX/RX automatically. */
    rc = gpio_pin_configure_dt(&radio_ant_switch, GPIO_OUTPUT_ACTIVE);
    if (rc != 0) return rc;
    rc = gpio_pin_configure_dt(&imu_int, GPIO_INPUT);
    if (rc != 0) return rc;
    for (size_t i = 0; i < ARRAY_SIZE(state_trace_pins); ++i) {
        if (!gpio_is_ready_dt(&state_trace_pins[i])) return -ENODEV;
        rc = gpio_pin_configure_dt(&state_trace_pins[i], GPIO_OUTPUT_INACTIVE);
        if (rc != 0) return rc;
    }
    gpio_init_callback(&imu_gpio_cb, tag_gpio_isr, BIT(imu_int.pin));
    rc = gpio_add_callback(imu_int.port, &imu_gpio_cb);
    if (rc != 0) return rc;
    rc = gpio_pin_interrupt_configure_dt(&imu_int, GPIO_INT_EDGE_TO_ACTIVE);
    if (rc != 0) return rc;
    gpio_init_callback(&radio_gpio_cb, tag_gpio_isr, BIT(radio_dio1.pin));
    rc = gpio_add_callback(radio_dio1.port, &radio_gpio_cb);
    if (rc != 0) return rc;
    rc = gpio_pin_interrupt_configure_dt(&radio_dio1, GPIO_INT_EDGE_TO_ACTIVE);
    if (rc != 0) return rc;
    rc = imu_configure();
    if (rc != 0) {
        LOG_ERR("LIS2DW12 I2C setup failed (%d)", rc);
        return rc;
    }

    const tag_hal_t hal = {
        .context = NULL,
        .spi_transfer = spi_transfer,
        .radio_reset = radio_reset_fn,
        .radio_busy = radio_busy_fn,
        .imu_read = imu_read_fn,
        .imu_irq_pending = imu_irq_pending,
        .radio_irq_pending = radio_irq_pending,
        .clock_ms = clock_ms,
        .clock_us = clock_us,
        .sleep_ms = sleep_ms,
        .wait_for_event = wait_for_event,
        .state_trace = state_trace,
        .trace_event = IS_ENABLED(CONFIG_TAG_STRUCTURED_TRACE) ? trace_event : NULL,
    };
    tag_config_t config = tag_default_config(CONFIG_TAG_ID);
    config.rf_frequency_hz = CONFIG_TAG_RF_FREQUENCY_HZ;
    config.tx_power_dbm = CONFIG_TAG_TX_POWER_DBM;
    config.battery_mv = CONFIG_TAG_BATTERY_MV;
    static tag_firmware_t firmware;
    rc = tag_firmware_init(&firmware, &hal, &config);
    if (rc != 0) return rc;
    if (reset_cause_available && previous_reset_kind != TAG_RESET_KIND_POWER_ON) {
        const tag_trace_event_t event = previous_reset_kind == TAG_RESET_KIND_WATCHDOG
            ? TAG_TRACE_WATCHDOG : TAG_TRACE_REBOOT;
        tag_trace_emit(&firmware.hal, firmware.state, event, TAG_TRACE_SOURCE_HAL,
                       0, previous_reset_cause, (uint32_t)previous_reset_kind, 0u);
    }
    LOG_INF("C tag firmware started (tag id %u)", config.tag_id);

    while (true) {
        tag_firmware_step(&firmware);
        rc = watchdog_feed();
        if (rc != 0) {
            LOG_ERR("Watchdog feed failed after firmware step (%d)", rc);
            return rc;
        }
    }
    return 0;
}
