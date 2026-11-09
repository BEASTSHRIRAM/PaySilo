import { Loader2 } from "lucide-react";

import { LottieAnimation } from "@/components/brand/lottie";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

export function SectionLoading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-3 py-10 font-bold text-sage-400" role="status">
      <Loader2 className="size-5 animate-spin" /> {label}
    </div>
  );
}

export function SectionError({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="grid gap-3 rounded-3xl border-2 border-coral-600/60 bg-coral-500/10 px-4 py-4" role="alert">
      <p className="text-sm font-bold text-coral-300">{message}</p>
      {onRetry && (
        <div>
          <Button type="button" variant="outline" size="sm" onClick={onRetry}>
            Try again
          </Button>
        </div>
      )}
    </div>
  );
}

export function SectionEmpty({ title, body, lottie = true }: { title: string; body: string; lottie?: boolean }) {
  return (
    <Card className="flex flex-col items-center gap-2 p-8 text-center">
      {lottie && <LottieAnimation name="silo-idle" className="size-24" label="Silo" />}
      <p className="font-bold text-sand-100">{title}</p>
      <p className="max-w-md text-sm text-sage-400">{body}</p>
    </Card>
  );
}

export function PageError({ message }: { message: string }) {
  return (
    <p role="alert" className="rounded-2xl border-2 border-coral-600/60 bg-coral-500/10 px-4 py-3 text-sm font-bold text-coral-300">
      {message}
    </p>
  );
}

export function PageNotice({ message }: { message: string }) {
  return (
    <p role="status" className="rounded-2xl border-2 border-sage-600/60 bg-sage-500/10 px-4 py-3 text-sm font-bold text-sage-300">
      {message}
    </p>
  );
}
