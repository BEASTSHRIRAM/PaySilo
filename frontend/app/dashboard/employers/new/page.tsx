"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft, ArrowRight, CheckCircle2, Loader2, Plus, Wallet } from "lucide-react";

import { LottieAnimation } from "@/components/brand/lottie";
import { AppBar } from "@/components/dashboard/app-bar";
import { PageError } from "@/components/dashboard/states";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, ApiError, type EmployerDetail, type Session } from "@/lib/api";
import { EDITOR_ROLES } from "@/lib/status";

export default function NewEmployerPage() {
  const router = useRouter();
  const [session, setSession] = useState<Session | null>(null);
  const [created, setCreated] = useState<EmployerDetail | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [form, setForm] = useState({ name: "", legal_name: "", country: "", contact_name: "", contact_email: "" });
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  useEffect(() => {
    (async () => {
      try {
        setSession(await api.me());
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) router.replace("/login?next=/dashboard/employers/new");
      }
    })();
  }, [router]);

  const canEdit = !session || EDITOR_ROLES.includes(session.role);
  const valid = form.name.trim().length >= 2 && /^[A-Za-z]{2}$/.test(form.country.trim());

  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    if (!valid || !canEdit) return;
    setSaving(true);
    setError(null);
    setFieldErrors({});
    try {
      const emp = await api.createEmployer({
        name: form.name.trim(),
        legal_name: form.legal_name.trim() || null,
        country: form.country.trim().toUpperCase(),
        contact_name: form.contact_name.trim() || null,
        contact_email: form.contact_email.trim() || null,
        funding_currency: "USD",
      });
      setCreated(emp);
    } catch (err) {
      if (err instanceof ApiError) {
        setFieldErrors(err.fields);
        setError(err.message);
      } else {
        setError("Could not create the employer. Try again.");
      }
    } finally {
      setSaving(false);
    }
  }

  const fieldErr = (name: string) =>
    fieldErrors[name] ? <p className="mt-1 text-sm font-bold text-coral-300">{fieldErrors[name]}</p> : null;

  return (
    <div className="min-h-dvh pb-16">
      {session ? <AppBar session={session} /> : <div className="h-[76px]" />}

      <main className="mx-auto mt-8 grid max-w-3xl gap-8 px-4 sm:px-6">
        <div>
          <Link href="/dashboard" className="inline-flex items-center gap-2 font-bold text-sage-300 hover:text-sand-100">
            <ArrowLeft className="size-4" /> All employers
          </Link>
        </div>

        {created ? (
          <Card className="flex flex-col items-center gap-3 p-8 text-center">
            <LottieAnimation name="silo-cheer" className="size-32" label="Silo celebrating" loop={false} />
            <h1 className="font-display text-3xl font-semibold text-sand-100">{created.name} is ready</h1>
            <p className="max-w-md text-sage-300">
              Its own silo and connected account were created. Money added here can only ever pay this employer&apos;s contractors.
            </p>
            <div className="mt-2 grid w-full gap-3 text-left sm:grid-cols-2">
              <Link
                href={`/dashboard/employers/${created.id}#contractors`}
                className="flex items-center justify-between gap-3 rounded-3xl border-2 border-ink-700 bg-ink-900/50 p-4 font-bold text-sand-100 transition-colors hover:border-sage-600"
              >
                <span className="flex items-center gap-2"><CheckCircle2 className="size-5 text-sage-400" /> 1 · Add contractors</span>
                <ArrowRight className="size-5 text-sage-400" />
              </Link>
              <Link
                href={`/dashboard/employers/${created.id}`}
                className="flex items-center justify-between gap-3 rounded-3xl border-2 border-ink-700 bg-ink-900/50 p-4 font-bold text-sand-100 transition-colors hover:border-sage-600"
              >
                <span className="flex items-center gap-2"><Wallet className="size-5 text-sage-400" /> 2 · Add funds (demo)</span>
                <ArrowRight className="size-5 text-sage-400" />
              </Link>
            </div>
            <Button asChild className="mt-2">
              <Link href={`/dashboard/employers/${created.id}`}>Open {created.name}</Link>
            </Button>
          </Card>
        ) : !canEdit ? (
          <Card className="p-8 text-center">
            <h1 className="font-display text-2xl font-semibold text-sand-100">Adding employers needs an editor</h1>
            <p className="mx-auto mt-2 max-w-md text-sage-300">
              Your role ({session?.role}) is read-only here. Ask an owner or admin to add the employer, or to change your role.
            </p>
            <Button asChild className="mt-4"><Link href="/dashboard">Back to the dashboard</Link></Button>
          </Card>
        ) : (
          <>
            <section>
              <h1 className="font-display text-3xl font-semibold text-sand-100 sm:text-4xl">Add an employer</h1>
              <p className="mt-1 text-sage-300">
                Each employer gets its own silo and connected account, so its money never mixes with anyone else&apos;s.
              </p>
            </section>

            <Card className="p-5 sm:p-6">
              <form onSubmit={(e) => void submit(e)} className="grid gap-4" noValidate>
                {error && <PageError message={error} />}
                <div>
                  <Label htmlFor="e-name">Employer name</Label>
                  <Input id="e-name" value={form.name} onChange={set("name")} required className="mt-1.5" aria-invalid={!!fieldErrors.name} placeholder="Borealis Labs AB" />
                  {fieldErr("name")}
                </div>
                <div>
                  <Label htmlFor="e-legal">Legal name (optional)</Label>
                  <Input id="e-legal" value={form.legal_name} onChange={set("legal_name")} className="mt-1.5" aria-invalid={!!fieldErrors.legal_name} />
                  {fieldErr("legal_name")}
                </div>
                <div className="grid gap-4 sm:grid-cols-2">
                  <div>
                    <Label htmlFor="e-country">Country (2-letter code)</Label>
                    <Input id="e-country" value={form.country} onChange={set("country")} placeholder="SE" maxLength={2} required className="mt-1.5" aria-invalid={!!fieldErrors.country} />
                    {fieldErr("country")}
                  </div>
                  <div>
                    <Label htmlFor="e-currency">Funding currency</Label>
                    <Input id="e-currency" value="USD" disabled className="mt-1.5" />
                    <p className="mt-1 text-sm text-sage-400">Employer wallets are funded in USD in this release.</p>
                  </div>
                  <div>
                    <Label htmlFor="e-contact">Contact name (optional)</Label>
                    <Input id="e-contact" value={form.contact_name} onChange={set("contact_name")} className="mt-1.5" aria-invalid={!!fieldErrors.contact_name} />
                    {fieldErr("contact_name")}
                  </div>
                  <div>
                    <Label htmlFor="e-email">Contact email (optional)</Label>
                    <Input id="e-email" type="email" value={form.contact_email} onChange={set("contact_email")} className="mt-1.5" aria-invalid={!!fieldErrors.contact_email} />
                    {fieldErr("contact_email")}
                  </div>
                </div>
                <div className="flex flex-wrap gap-3 pt-1">
                  <Button type="submit" disabled={!valid || saving}>
                    {saving ? <Loader2 className="animate-spin" /> : <Plus />} Create employer
                  </Button>
                  <Button type="button" variant="ghost" onClick={() => router.push("/dashboard")}>Cancel</Button>
                </div>
              </form>
            </Card>
          </>
        )}
      </main>
    </div>
  );
}
