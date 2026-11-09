"use client";
import { FormEvent, useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { api, type PublicInvitation } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export default function InvitePage() {
  const params = useParams<{ token: string }>(); const router = useRouter(); const token = params.token;
  const [invite, setInvite] = useState<PublicInvitation | null>(null); const [invalid, setInvalid] = useState(false);
  const [name, setName] = useState(""); const [password, setPassword] = useState(""); const [confirm, setConfirm] = useState(""); const [error, setError] = useState("");
  useEffect(() => { api.publicInvitation(token).then(setInvite).catch(() => setInvalid(true)); }, [token]);
  async function accept(e: FormEvent) {
    e.preventDefault(); setError("");
    if (!invite?.existing_user && password !== confirm) { setError("Passwords do not match."); return; }
    try { await api.acceptInvitation(token, invite?.existing_user ? { password } : { full_name: name, password }); router.replace("/dashboard"); }
    catch (e) { setError((e as Error).message); }
  }
  return <main className="grid min-h-screen place-items-center bg-ink-950 px-4 py-12"><Card className="w-full max-w-lg"><CardHeader><CardTitle>{invalid ? "Invitation unavailable" : invite ? `Join ${invite.organization_name}` : "Loading invitation…"}</CardTitle></CardHeader><CardContent>
    {invalid ? <div className="grid gap-3 text-sage-300"><p>This invitation is invalid, expired, or has already been used.</p><Link className="text-coral-400 underline" href="/login">Go to login</Link></div> : invite && <>
      <p className="mb-5 text-sm text-sage-300">{invite.invited_by ? `${invite.invited_by} invited you` : "You were invited"} as {invite.role} at {invite.organization_name}. Invitation for {invite.email}.</p>
      <form onSubmit={accept} className="grid gap-4">{!invite.existing_user && <div><Label htmlFor="name">Full name</Label><Input id="name" value={name} onChange={e => setName(e.target.value)} minLength={2} required /></div>}
        <div><Label htmlFor="password">{invite.existing_user ? "Account password" : "Create password"}</Label><Input id="password" type="password" value={password} onChange={e => setPassword(e.target.value)} minLength={invite.existing_user ? 1 : 8} required /></div>
        {!invite.existing_user && <div><Label htmlFor="confirm">Confirm password</Label><Input id="confirm" type="password" value={confirm} onChange={e => setConfirm(e.target.value)} required /></div>}
        {error && <p role="alert" className="text-coral-400">{error}</p>}<Button>Join {invite.organization_name}</Button></form></>}
  </CardContent></Card></main>;
}
