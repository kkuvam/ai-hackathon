import numpy as np
import pandas as pd
from scipy import signal


def _resample_imu(imu_df: pd.DataFrame, target_hz: float = 50.0) -> pd.DataFrame:
    """Resample IMU data to target frequency using linear interpolation."""
    if len(imu_df) < 2:
        return imu_df

    start_t = imu_df["t"].iloc[0]
    end_t = imu_df["t"].iloc[-1]
    interval_ms = 1000.0 / target_hz

    new_t = np.arange(start_t, end_t, interval_ms)
    new_df = pd.DataFrame({"t": new_t})

    for col in ["ax", "ay", "az", "gx", "gy", "gz"]:
        new_df[col] = np.interp(new_t, imu_df["t"], imu_df[col])

    new_df["time"] = pd.to_datetime(new_df["t"], unit="ms", utc=True)
    return new_df


def _remove_gravity(imu_df: pd.DataFrame, window_s: float = 10.0, fs: float = 50.0) -> pd.DataFrame:
    """
    Remove gravity/DC offset by subtracting a rolling median from each raw axis.
    Orientation-independent: works however the phone is mounted.
    The accelerometer median (gravity) is kept as grav_x/grav_y/grav_z for the car-frame step.
    """
    imu_df = imu_df.copy()
    window = int(fs * window_s)
    for col in ["ax", "ay", "az", "gx", "gy", "gz"]:
        if col in imu_df.columns:
            rolling_median = imu_df[col].rolling(window=window, center=True, min_periods=1).median()
            if col in ("ax", "ay", "az"):
                imu_df[f"grav_{col[1]}"] = rolling_median
            imu_df[col] = imu_df[col] - rolling_median
    return imu_df


def _gravity_unit(imu_df: pd.DataFrame) -> np.ndarray:
    """Per-sample unit vector along gravity, from the grav_* columns set by _remove_gravity."""
    grav = imu_df[["grav_x", "grav_y", "grav_z"]].to_numpy(dtype=float)
    norm = np.linalg.norm(grav, axis=1, keepdims=True)
    return np.divide(grav, norm, out=np.zeros_like(grav), where=norm > 0)


def _rotate_to_car_frame(imu_df: pd.DataFrame, gps_df: pd.DataFrame) -> pd.DataFrame:
    """
    Derive car-frame acceleration channels (all in g, matching the event thresholds):
    - accel_forward: dv/dt from GPS speed over ~2s windows, interpolated onto the IMU timeline
    - accel_lateral: yaw rate (gravity-free gyro about the gravity axis, rad/s) x GPS speed (m/s),
      so corners are found however the phone is mounted
    - accel_vertical: gravity-free acceleration along the gravity axis
    """
    imu_df = imu_df.copy()

    gps = gps_df.copy()
    gps["speed"] = gps["speed"].fillna(0)
    gps = gps.sort_values("t")
    imu_t = imu_df["t"].values.astype(float)

    if len(gps) >= 2:
        gps_t = gps["t"].values.astype(float)
        speeds = gps["speed"].values.astype(float)

        # dv/dt over a ~2s window centered on each GPS point (m/s^2)
        window_s = 2.0
        half_ms = window_s / 2.0 * 1000.0
        dv = np.interp(gps_t + half_ms, gps_t, speeds) - np.interp(gps_t - half_ms, gps_t, speeds)
        accel_forward_gps = dv / window_s

        accel_forward_ms2 = np.interp(imu_t, gps_t, accel_forward_gps)
        speed_on_imu = np.interp(imu_t, gps_t, speeds)
    else:
        accel_forward_ms2 = np.zeros(len(imu_df))
        speed_on_imu = np.zeros(len(imu_df))

    # Project onto the gravity axis so the result does not depend on how the phone is mounted
    up = _gravity_unit(imu_df)
    yaw = np.sum(imu_df[["gx", "gy", "gz"]].to_numpy(dtype=float) * up, axis=1)
    vertical = np.sum(imu_df[["ax", "ay", "az"]].to_numpy(dtype=float) * up, axis=1)

    # Convert m/s^2 to g so the g-unit thresholds apply
    imu_df["accel_forward"] = accel_forward_ms2 / 9.81
    imu_df["accel_lateral"] = yaw * speed_on_imu / 9.81
    imu_df["accel_vertical"] = vertical
    return imu_df


def _apply_lowpass_filter(
    imu_df: pd.DataFrame, cutoff_hz: float = 5.0, fs: float = 50.0
) -> pd.DataFrame:
    """Apply Butterworth low-pass filter to remove road vibration."""
    nyquist = fs / 2.0
    normal_cutoff = cutoff_hz / nyquist
    b, a = signal.butter(4, normal_cutoff, btype="low", analog=False)

    imu_df = imu_df.copy()
    for col in ["accel_forward", "accel_lateral", "accel_vertical"]:
        imu_df[col] = signal.filtfilt(b, a, imu_df[col])

    return imu_df
