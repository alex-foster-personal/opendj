use numpy::PyReadonlyArray1;
use pyo3::exceptions::PyTypeError;
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
    let points = usize::try_from(points).unwrap_or(0);
    let mut length = 0usize;

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
        let values: Vec<f64> = view.iter().copied().collect();
        let materialized = downsample_max_rounded(&values, points);
        length = materialized.len();
        output.set_item(name, PyList::new(py, materialized)?)?;
    }
    output.set_item("length", length)?;
    Ok(output)
}

#[pymodule]
fn _waveform_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(bands_payload, module)?)?;
    module.add("BACKEND", "rust-pyo3")?;
    Ok(())
}
