import React, { useEffect, useState } from "react";
import { View, Text, Pressable, StyleSheet } from "react-native";
import { api } from "../lib/api";

type State = { release_available: boolean; permission: string; day: string | null;
  checks: { checked_at: string; purpose: string; state: string; stage: string }[] };
const stages: Record<string, string> = {
  "signal-source": "Signal connection and freshness", "market-status": "Broker market status",
  "entry-quote": "Contract quote", "risk-marks": "Open-position prices",
  "cash-risk-and-margin": "Cash allocation, limits and broker margin",
  "final-recheck": "Final freshness and account checks", "entry-authorization": "Entry permission",
  "read-only-check-complete": "Read-only check complete", "entry-acknowledgement": "Broker acknowledgement; fill unconfirmed",
  starting: "Check started",
};

export default function LiveEntries({ uid, version }: { uid: string; version: number | null }) {
  const [state, setState] = useState<State | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  useEffect(() => {
    let current = true;
    const refresh = () => api("/live/entries").then((v) => { if (current) setState(v); })
      .catch(() => { if (current) setState(null); });
    void refresh(); const timer = setInterval(() => { void refresh(); }, 10000);
    return () => { current = false; clearInterval(timer); };
  }, [uid]);
  async function authorize() {
    setBusy(true); setConfirm(false); setNotice("");
    try {
      await api("/live/entries/authorize", { policy_version: version,
        confirmation: "AUTHORIZE AUTOMATIC LIVE ENTRIES THIS INDIA DAY WITH MY REVIEWED LIMITS" });
      setNotice("Automatic entries authorized for this India trading day under your reviewed limits. No confirmation is needed for each signal. Permission ends at India midnight, on pause, worker restart or a blocking failure.");
      setState(await api("/live/entries"));
    } catch (e) { setNotice(e instanceof Error ? e.message : "Authorization failed."); }
    finally { setBusy(false); }
  }
  return <View style={s.card}>
    <Text style={s.title}>Live entry checks</Text>
    <Text style={s.text}>While flat, your worker can check new signals against broker quotes, cash limits and margin without placing an order. A passed check is not permission to trade or proof of spendable cash.</Text>
    {!state?.release_available && <Text style={s.text}>Real-money entry authorization is locked pending release acceptance.</Text>}
    <Text style={s.text}>Current scope: sequential positions only; sale proceeds are not reused the same day. This does not yet reproduce Paper trading frequency. Session authorization requires a running, reconciled worker and ends at India midnight.</Text>
    {state?.day && <Text style={s.text}>Last permission: {state.permission} · India date {state.day}</Text>}
    {notice ? <Text accessibilityRole="alert" style={s.text}>{notice}</Text> : null}
    {state?.release_available && version ? <>
      {!confirm ? <Pressable accessibilityRole="button" disabled={busy} style={s.button} onPress={() => setConfirm(true)}>
        <Text style={s.label}>Review automatic trading session</Text></Pressable> : <>
        <Text style={s.text}>Authorize automatic real orders from eligible signals for the rest of this India trading day, using your reviewed limits and cash allocation. You may be asleep when orders are placed and money can be lost. Each order is checked independently. Unknown outcomes stop new entries and are never automatically retried.</Text>
        <Pressable accessibilityRole="button" disabled={busy} style={s.button} onPress={authorize}>
          <Text style={s.label}>Authorize this Live trading session</Text></Pressable>
        <Pressable accessibilityRole="button" onPress={() => setConfirm(false)}><Text style={s.text}>Cancel review</Text></Pressable>
      </>}
    </> : null}
    {state?.checks.map((check, i) => <View key={check.checked_at + i}>
      <Text style={s.text}>{new Date(check.checked_at).toLocaleString()} · {check.purpose === "entry" ? "Entry attempt" : "Read-only check"}</Text>
      <Text style={s.text}>{check.state} · {stages[check.stage] || "Review required"}</Text>
    </View>)}
  </View>;
}
const s = StyleSheet.create({
  card: { gap: 12, paddingVertical: 12 }, title: { color: "#e3edf7", fontSize: 20, fontWeight: "700" },
  text: { color: "#b8cadd", fontSize: 15, lineHeight: 22 },
  button: { borderRadius: 12, padding: 14, backgroundColor: "#51dfc4" },
  label: { color: "#062722", textAlign: "center", fontWeight: "700" },
});
