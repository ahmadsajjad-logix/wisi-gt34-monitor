from __future__ import annotations

from .models import ReceiverSample


def derive_rf_band(frequency_khz: int | float | None) -> tuple[str | None, str | None]:
    """Conservative RF-band classification from configured RF frequency only.

    This is intentionally NOT presented as a textual WISI LNB/band field.
    Provenance is returned with the value so stored history remains explicit.
    """
    if frequency_khz is None:
        return None, None

    ghz = float(frequency_khz) / 1_000_000.0
    if 3.4 <= ghz <= 4.8:
        return "C-band", "DERIVED_FROM_RF_FREQUENCY"
    if 10.0 <= ghz <= 13.5:
        return "Ku-band", "DERIVED_FROM_RF_FREQUENCY"
    return "Other/unknown", "DERIVED_FROM_RF_FREQUENCY"


def valid_configured_mis(value: int | None) -> int | None:
    """Return a display-safe MIS/ISI value only when it is in the 8-bit ISI range.

    Values outside 0..255 are preserved in raw history but are deliberately not
    labelled as an operational MIS/ISI selection in PRTG until vendor semantics
    are proven.  This specifically prevents raw sentinel-like values (e.g. 256)
    from being misrepresented to operators.
    """
    if value is None:
        return None
    try:
        integer = int(value)
    except (TypeError, ValueError):
        return None
    return integer if 0 <= integer <= 255 else None


def transponder_status_parts(sample: ReceiverSample) -> list[str]:
    labels = sample.labels()
    parts: list[str] = []

    band, _provenance = derive_rf_band(sample.frequency_khz)

    freq = (
        f"{sample.frequency_khz / 1000.0:g} MHz"
        if sample.frequency_khz is not None
        else "freq N/A"
    )
    pol = {
        "horizontal": "H",
        "vertical": "V",
        "circular-left": "CL",
        "circular-right": "CR",
    }.get(labels.get("polarisation") or "", "")
    sr = (
        f"{sample.symbol_rate_bd / 1000.0:g} kBaud"
        if sample.symbol_rate_bd is not None
        else "SR N/A"
    )

    tp_items = [freq]
    if pol:
        tp_items.append(pol)
    tp_items.append(sr)
    if band:
        suffix = "(derived)" if _provenance == "DERIVED_FROM_RF_FREQUENCY" else ""
        tp_items.append(f"{band}{suffix}")
    parts.append("TP " + " / ".join(tp_items))

    cfg_items: list[str] = []
    mod = labels.get("modulation")
    fec = labels.get("code_rate")
    if mod:
        cfg_items.append(f"MOD {mod.upper()}")
    elif sample.modulation is not None:
        cfg_items.append(f"MOD ENUM {sample.modulation}")
    if fec:
        cfg_items.append(f"FEC {fec.upper()}")
    elif sample.code_rate is not None:
        cfg_items.append(f"FEC ENUM {sample.code_rate}")

    mis = valid_configured_mis(sample.mis)
    if mis is not None:
        cfg_items.append(f"MIS {mis}")

    pls_mode = labels.get("pls_mode")
    if pls_mode:
        cfg_items.append(f"PLS {pls_mode.upper()}")
    if sample.pls_id is not None:
        cfg_items.append(f"PLS-ID {sample.pls_id}")
    if cfg_items:
        parts.append("CFG " + " ".join(cfg_items))

    det_items: list[str] = []
    if sample.detected_constellation:
        det_items.append(sample.detected_constellation.upper())
    if sample.detected_code_rate:
        det_items.append(sample.detected_code_rate.upper())
    if sample.detected_isi is not None:
        det_items.append(f"ISI {sample.detected_isi}")
    if det_items:
        parts.append("DET " + " / ".join(det_items))

    lnb_items: list[str] = []
    if sample.lnb_type is not None:
        lnb_label = labels.get("lnb_type")
        if lnb_label:
            lnb_items.append(f"{lnb_label.upper()}(RAW {sample.lnb_type})")
        else:
            lnb_items.append(f"RAW {sample.lnb_type}")
    if sample.lo_frequency_khz is not None and sample.lo_frequency_khz > 0:
        lnb_items.append(f"LO {sample.lo_frequency_khz / 1000.0:g} MHz")
    voltage = labels.get("lnb_voltage")
    if voltage:
        lnb_items.append(f"V {voltage.upper()}")
    tone = labels.get("tone_22khz")
    if tone:
        lnb_items.append(f"22K {tone.upper()}")
    if lnb_items:
        parts.append("LNB " + " ".join(lnb_items))

    if sample.web_if_frequency_khz is not None:
        parts.append(f"IF {sample.web_if_frequency_khz / 1000.0:g} MHz")

    return parts
