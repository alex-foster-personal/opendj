use numpy::PyReadonlyArray1;
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

const DECIMAL_SCALE: f64 = 10_000.0;

/// Round the normalized waveform values to the four decimal places carried by
/// the JSON contract. Real ANLZ bands are integer / 127 or integer / 31, so no
/// value is a decimal half-tie at this precision; ties-to-even still matches
/// Python's documented rounding rule for other finite inputs.
#[inline]
fn round_four(value: f64) -> f64 {
    if value.is_finite() {
        (value * DECIMAL_SCALE).round_ties_even() / DECIMAL_SCALE
    } else {
        value
    }
}

#[inline]
fn numpy_max(accumulator: f64, value: f64) -> f64 {
    // np.maximum propagates NaN. Rust's f64::max deliberately does not.
    if accumulator.is_nan() || value.is_nan() {
        f64::NAN
    } else {
        accumulator.max(value)
    }
}

fn downsample_max_rounded(values: &[f64], points: usize) -> Vec<f64> {
    let len = values.len();
    if points == 0 {
        return Vec::new();
    }
    if len <= points {
        return values.iter().copied().map(round_four).collect();
    }

    let mut output = Vec::with_capacity(points);
    for bucket in 0..points {
        let start = bucket * len / points;
        let end = if bucket + 1 == points {
            len
        } else {
            (bucket + 1) * len / points
        };
        let mut maximum = values[start];
        for &value in &values[start + 1..end] {
            maximum = numpy_max(maximum, value);
        }
        output.push(round_four(maximum));
    }
    output
}

/// Materialize the existing rb_vendor waveform payload from decoded NumPy
/// float64 band arrays. Arrays may be strided: PWV6/PWV7 production arrays are
/// column views with a 24-byte stride rather than contiguous copies.
#[pyfunction]
fn bands_payload<'py>(
    py: Python<'py>,
    bands: &Bound<'py, PyDict>,
    points: isize,
) -> PyResult<Bound<'py, PyDict>> {
    let output = PyDict::new(py);
    // The HTTP boundary requires >=100. Keep the legacy helper's internal
    // zero/negative behaviour exact: NumPy returns an empty payload there.
    let points = usize::try_from(points).unwrap_or(0);
    let mut length = 0usize;
    let mut expected_band_length = None;

    for (name, value) in bands.iter() {
        let name_text = name
            .extract::<String>()
            .map_err(|_| PyTypeError::new_err("waveform band names must be strings"))?;
        let array = value.extract::<PyReadonlyArray1<'_, f64>>().map_err(|_| {
            PyTypeError::new_err(format!(
                "waveform band '{name_text}' must be a NumPy 1-D float64 array"
            ))
        })?;
        let view = array.as_array();
        let band_length = view.len();
        if let Some(expected) = expected_band_length {
            if band_length != expected {
                return Err(PyValueError::new_err(format!(
                    "waveform bands must have equal lengths; band '{name_text}' has {band_length}, expected {expected}"
                )));
            }
        } else {
            expected_band_length = Some(band_length);
        }
        let values: Vec<f64> = view.iter().copied().collect();
        // Array extraction and Python-list construction require the GIL. The
        // pure numeric kernel does not, and sync FastAPI routes can overlap in
        // the server's worker pool, so let other requests run during it.
        let materialized = py.allow_threads(move || downsample_max_rounded(&values, points));
        length = materialized.len();
        output.set_item(name, PyList::new(py, materialized)?)?;
    }
    output.set_item("length", length)?;
    Ok(output)
}

#[pymodule]
fn _rb_waveform_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(bands_payload, module)?)?;
    module.add("BACKEND", "rust-pyo3")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::downsample_max_rounded;

    #[test]
    fn uneven_buckets_follow_numpy_reduceat_edges() {
        // floor([0, 7/3, 14/3]) => starts [0, 2, 4], hence 2/2/3 items.
        assert_eq!(
            downsample_max_rounded(&[1.0, 7.0, 3.0, 4.0, 2.0, 9.0, 5.0], 3),
            vec![7.0, 4.0, 9.0]
        );
    }

    #[test]
    fn no_downsample_still_rounds_to_payload_precision() {
        assert_eq!(
            downsample_max_rounded(&[0.123_456, 0.000_05, 1.0], 8),
            vec![0.1235, 0.0, 1.0]
        );
    }

    #[test]
    fn nan_propagates_within_a_bucket_like_numpy_maximum() {
        let output = downsample_max_rounded(&[1.0, f64::NAN, 3.0, 2.0], 2);
        assert!(output[0].is_nan());
        assert_eq!(output[1], 3.0);
    }

    #[test]
    fn zero_points_and_empty_input_are_empty() {
        assert!(downsample_max_rounded(&[1.0, 2.0], 0).is_empty());
        assert!(downsample_max_rounded(&[], 100).is_empty());
    }
}
