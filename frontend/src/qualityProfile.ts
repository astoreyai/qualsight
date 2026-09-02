export type QualityMeasureGroupId =
  | "overall"
  | "capture-illumination"
  | "face-geometry-crop-pose"
  | "visibility-occlusion-expression"
  | "additional-recorded";

export interface QualityMeasureGroupDefinition {
  id: Exclude<QualityMeasureGroupId, "additional-recorded">;
  label: string;
  measureNames: readonly string[];
}

export interface GroupedQualityMeasures<T> {
  id: QualityMeasureGroupId;
  label: string;
  measures: T[];
}

export const QUALITY_MEASURE_GROUPS: readonly QualityMeasureGroupDefinition[] = [
  {
    id: "overall",
    label: "Overall quality",
    measureNames: ["UnifiedQualityScore"],
  },
  {
    id: "capture-illumination",
    label: "Capture & illumination",
    measureNames: [
      "BackgroundUniformity",
      "IlluminationUniformity",
      "LuminanceMean",
      "LuminanceVariance",
      "UnderExposurePrevention",
      "OverExposurePrevention",
      "DynamicRange",
      "Sharpness",
      "CompressionArtifacts",
      "NaturalColour",
    ],
  },
  {
    id: "face-geometry-crop-pose",
    label: "Face geometry, crop & pose",
    measureNames: [
      "SingleFacePresent",
      "InterEyeDistance",
      "HeadSize",
      "LeftwardCropOfTheFaceImage",
      "RightwardCropOfTheFaceImage",
      "MarginAboveOfTheFaceImage",
      "MarginBelowOfTheFaceImage",
      "HeadPoseYaw",
      "HeadPosePitch",
      "HeadPoseRoll",
    ],
  },
  {
    id: "visibility-occlusion-expression",
    label: "Visibility, occlusion & expression",
    measureNames: [
      "EyesOpen",
      "MouthClosed",
      "EyesVisible",
      "MouthOcclusionPrevention",
      "FaceOcclusionPrevention",
      "ExpressionNeutrality",
      "NoHeadCoverings",
    ],
  },
] as const;

export function qualityMeasureLabel(name: string): string {
  return name.replace(/([a-z0-9])([A-Z])/g, "$1 $2");
}

export function groupRecordedQualityMeasures<T extends { name: string }>(
  recorded: readonly T[],
): GroupedQualityMeasures<T>[] {
  const byName = new Map(recorded.map((measure) => [measure.name, measure]));
  const grouped: GroupedQualityMeasures<T>[] = QUALITY_MEASURE_GROUPS.map((definition) => ({
    id: definition.id,
    label: definition.label,
    measures: definition.measureNames
      .map((name) => byName.get(name))
      .filter((measure): measure is T => measure !== undefined),
  })).filter((group) => group.measures.length > 0);
  const mapped = new Set(QUALITY_MEASURE_GROUPS.flatMap((definition) => definition.measureNames));
  const additional = recorded.filter((measure) => !mapped.has(measure.name));
  if (additional.length > 0) {
    grouped.push({
      id: "additional-recorded",
      label: "Additional recorded measures",
      measures: additional.slice(),
    });
  }
  return grouped;
}

export function groupRecordedQualityMeasureNames(
  recordedNames: readonly string[],
): GroupedQualityMeasures<string>[] {
  return groupRecordedQualityMeasures(recordedNames.map((name) => ({ name }))).map((group) => ({
    id: group.id,
    label: group.label,
    measures: group.measures.map((measure) => measure.name),
  }));
}
