import type { NumericParameterContract } from "./api";

export function numericParameterError(
  value: string,
  contract: NumericParameterContract | null,
  label: string,
): string | null {
  if (!contract) return `${label} contract is unavailable; retry the admitted add-on catalog.`;
  if (value.trim() === "") return contract.required ? `${label} is required.` : null;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return `${label} must be ${contract.guidance}.`;
  if (contract.value_type === "integer" && !Number.isInteger(parsed)) return `${label} must be ${contract.guidance}.`;
  if (contract.minimum !== undefined && (contract.minimum_exclusive ? parsed <= contract.minimum : parsed < contract.minimum)) return `${label} must be ${contract.guidance}.`;
  if (contract.maximum !== undefined && (contract.maximum_exclusive ? parsed >= contract.maximum : parsed > contract.maximum)) return `${label} must be ${contract.guidance}.`;
  return null;
}
