"use client";
import { FormEvent, useEffect, useState } from "react";
import { api, type Session } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { AppBar } from "@/components/dashboard/app-bar";

export default function ProfilePage() {
  const [session, setSession] = useState<Session | null>(null); const [name, setName] = useState("");
  const [current, setCurrent] = useState(""); const [next, setNext] = useState(""); const [confirm, setConfirm] = useState(""); const [message, setMessage] = useState(""); const [error, setError] = useState("");
  useEffect(() => { api.me().then(s => { setSession(s); setName(s.user.full_name); }).catch(e => setError(e.message)); }, []);
  async function saveName(e: FormEvent) { e.preventDefault(); setMessage(""); setError(""); try { const s = await api.updateMe(name); setSession(s); setMessage("Name updated."); } catch (e) { setError((e as Error).message); } }
  async function savePassword(e: FormEvent) { e.preventDefault(); setMessage(""); setError(""); if (next !== confirm) { setError("Passwords do not match."); return; } try { await api.changePassword(current, next); setCurrent(""); setNext(""); setConfirm(""); setMessage("Password changed."); } catch (e) { setError((e as Error).message); } }
  return <>{session && <AppBar session={session} />}<main className="mx-auto mt-8 grid max-w-3xl gap-6 px-4 pb-20 pt-12 sm:px-6"><header><h1 className="font-display text-3xl font-bold text-sand-100">Profile</h1><p className="mt-2 text-sage-300">Manage your account details and password.</p></header>
    {session && <><Card><CardHeader><CardTitle>Account details</CardTitle></CardHeader><CardContent><form onSubmit={saveName} className="grid gap-4"><div><Label htmlFor="full-name">Full name</Label><Input id="full-name" value={name} onChange={e=>setName(e.target.value)} minLength={2} maxLength={120} required /></div><div className="grid gap-1 text-sm text-sage-300"><span>Email · {session.user.email}</span><span>Role · {session.role}</span><span>Organization · {session.organization.name}</span></div><Button className="w-fit">Save name</Button></form></CardContent></Card>
    <Card><CardHeader><CardTitle>Change password</CardTitle></CardHeader><CardContent><form onSubmit={savePassword} className="grid gap-4"><div><Label htmlFor="current">Current password</Label><Input id="current" type="password" value={current} onChange={e=>setCurrent(e.target.value)} required /></div><div><Label htmlFor="new">New password</Label><Input id="new" type="password" value={next} onChange={e=>setNext(e.target.value)} minLength={8} required /></div><div><Label htmlFor="confirm">Confirm new password</Label><Input id="confirm" type="password" value={confirm} onChange={e=>setConfirm(e.target.value)} required /></div><Button className="w-fit">Change password</Button></form></CardContent></Card></>}
    {message && <p role="status" className="text-mint-400">{message}</p>}{error && <p role="alert" className="text-coral-400">{error}</p>}</main></>;
}
