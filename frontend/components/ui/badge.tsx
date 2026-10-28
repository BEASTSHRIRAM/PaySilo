import { HTMLAttributes } from "react";

type Tone = "success" | "warning" | "neutral" | "danger";

const tones: Record<Tone, string> = {
  success: "border-emerald-800 bg-emerald-950 text-emerald-300",
  warning: "border-amber-800 bg-amber-950 text-amber-300",
  neutral: "border-zinc-700 bg-zinc-900 text-zinc-300",
  danger: "border-red-900 bg-red-950 text-red-300",
};

export function toneForStatus(status: string): Tone {
  if (status === "SETTLED") return "success";
  if (status.startsWith("HELD")) return "warning";
  if (status === "REFUSED") return "danger";
  return "neutral";
}

/** shadcn-style badge, hand-rolled (no CLI). */
export function Badge({
  tone = "neutral",
  className = "",
  ...props
}: HTMLAttributes<HTMLSpanElement> & { tone?: Tone }) {
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium ${tones[tone]} ${className}`}
      {...props}
    />
  );
}
