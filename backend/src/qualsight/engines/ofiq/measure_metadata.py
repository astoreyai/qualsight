"""Native-value metadata derived from the admitted OFIQ 1.1.2 implementation.

OFIQ's scalar components share the ISO/IEC 29794-5 0--100 scale, but its
native values do not share a unit or numeric domain.  This closed registry
describes the calculation represented by each native column so API clients do
not guess a unit or plot heterogeneous values on one axis.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NativeMeasureMetadata:
    unit: str
    semantics: str


NATIVE_MEASURE_METADATA: dict[str, NativeMeasureMetadata] = {
    "UnifiedQualityScore": NativeMeasureMetadata(
        "model_score",
        "Output score from the OFIQ unified-quality neural network.",
    ),
    "BackgroundUniformity": NativeMeasureMetadata(
        "luminance_gradient_magnitude",
        "Mean gradient magnitude across the OFIQ background mask.",
    ),
    "IlluminationUniformity": NativeMeasureMetadata(
        "ratio",
        "Overlap of normalized luminance histograms for the two facial regions.",
    ),
    "LuminanceMean": NativeMeasureMetadata(
        "normalized_luminance",
        "Mean facial luminance after division of 8-bit luminance levels by 255.",
    ),
    "LuminanceVariance": NativeMeasureMetadata(
        "normalized_luminance_squared",
        "Variance of facial luminance after division of 8-bit luminance levels by 255.",
    ),
    "UnderExposurePrevention": NativeMeasureMetadata(
        "ratio",
        "Fraction of unoccluded facial pixels with luminance in the OFIQ dark range.",
    ),
    "OverExposurePrevention": NativeMeasureMetadata(
        "ratio",
        "Fraction of unoccluded facial pixels with luminance in the OFIQ light range.",
    ),
    "DynamicRange": NativeMeasureMetadata(
        "bit",
        "Shannon entropy of the facial luminance histogram using base-2 logarithms.",
    ),
    "Sharpness": NativeMeasureMetadata(
        "classifier_vote",
        "OFIQ random-forest focus classifier vote score.",
    ),
    "CompressionArtifacts": NativeMeasureMetadata(
        "model_score",
        "Output score from the OFIQ compression-artifact model.",
    ),
    "NaturalColour": NativeMeasureMetadata(
        "CIELAB_chroma_distance",
        "OFIQ distance computed from mean CIELAB a and b channels in facial regions.",
    ),
    "SingleFacePresent": NativeMeasureMetadata(
        "area_ratio",
        "Area ratio of the second-largest detected face to the largest detected face.",
    ),
    "EyesOpen": NativeMeasureMetadata(
        "ratio",
        "Smaller eye-opening distance divided by the OFIQ facial t-metric.",
    ),
    "MouthClosed": NativeMeasureMetadata(
        "ratio",
        "Maximum inner-mouth opening divided by the OFIQ facial t-metric.",
    ),
    "EyesVisible": NativeMeasureMetadata(
        "ratio",
        "Occluded-pixel fraction inside the OFIQ eye-visibility zones.",
    ),
    "MouthOcclusionPrevention": NativeMeasureMetadata(
        "ratio",
        "Occluded-pixel fraction inside the OFIQ mouth region.",
    ),
    "FaceOcclusionPrevention": NativeMeasureMetadata(
        "ratio",
        "Occluded-pixel fraction inside the OFIQ face region.",
    ),
    "InterEyeDistance": NativeMeasureMetadata(
        "pixel",
        "Pose-adjusted distance between the OFIQ eye centres in source-image pixels.",
    ),
    "HeadSize": NativeMeasureMetadata(
        "ratio",
        "OFIQ facial t-metric divided by source-image height.",
    ),
    "LeftwardCropOfTheFaceImage": NativeMeasureMetadata(
        "ratio",
        "Right-eye horizontal position divided by inter-eye distance.",
    ),
    "RightwardCropOfTheFaceImage": NativeMeasureMetadata(
        "ratio",
        "Distance from the left eye to the right image edge divided by inter-eye distance.",
    ),
    "MarginAboveOfTheFaceImage": NativeMeasureMetadata(
        "ratio",
        "Eye-midpoint vertical position divided by the facial t-metric.",
    ),
    "MarginBelowOfTheFaceImage": NativeMeasureMetadata(
        "ratio",
        "Distance from the eye midpoint to the lower image edge divided by the facial t-metric.",
    ),
    "HeadPoseYaw": NativeMeasureMetadata(
        "degree",
        "OFIQ estimated head-pose yaw angle.",
    ),
    "HeadPosePitch": NativeMeasureMetadata(
        "degree",
        "OFIQ estimated head-pose pitch angle.",
    ),
    "HeadPoseRoll": NativeMeasureMetadata(
        "degree",
        "OFIQ estimated head-pose roll angle.",
    ),
    "ExpressionNeutrality": NativeMeasureMetadata(
        "classifier_vote",
        "OFIQ expression-neutrality classifier vote sum.",
    ),
    "NoHeadCoverings": NativeMeasureMetadata(
        "ratio",
        "Fraction of the evaluated crop segmented as clothing or headwear.",
    ),
}


def native_measure_metadata(name: str) -> NativeMeasureMetadata:
    """Return the admitted metadata or fail closed for an unknown measure."""

    try:
        return NATIVE_MEASURE_METADATA[name]
    except KeyError as error:
        raise ValueError(f"OFIQ measure {name!r} has no admitted native metadata") from error


def observed_measure_failure_reason(
    *, name: str, native: float | None, scalar: int | None, failed: bool
) -> str | None:
    """Describe unavailable output fields without claiming an internal cause."""

    if not failed:
        return None
    unavailable: list[str] = []
    if native is None:
        unavailable.append("finite native value")
    if scalar is None:
        unavailable.append("scalar quality component")
    if unavailable:
        return f"OFIQ returned no {' or '.join(unavailable)} for {name}."
    return f"OFIQ marked {name} as failed despite returning both output fields."
