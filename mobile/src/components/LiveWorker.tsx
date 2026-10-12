import React, { useEffect, useState } from "react";
import { View, Text, TextInput, Pressable, StyleSheet } from "react-native";
import { api } from "../lib/api";

type Status = { reserved: boolean; worker_available: boolean; stop_requested: boolean };

export default function LiveWorker({ uid, version, reviewed }: {
  uid: string; version: number | null; reviewed: boolean;
}) {
  const [status, setStatus] = useState<Status | null>(null);
  const [code, setCode] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  useEffect(() => {
    let current = true;
    const refresh = () => api("/live/worker").then((value) => { if (current) setStatus(value); })
      .catch(() => { if (current) setStatus(null); });
    void refresh();
    const timer = setInterval(() => { void refresh(); }, 5000);
    return () => { current = false; clearInterval(timer); };
  }, [uid]);

  async function start() {
    const totp = code; setCode(""); setConfirm(false); setBusy(true); setNotice("");
    try {
      const value = await api("/live/worker/start", { totp, policy_version: version,
        confirmation: "START LIVE MONITORING; ENTRIES REMAIN PAUSED" });
      setNotice(value.state === "started-paused"
        ? "Worker authenticated. Entries remain paused. Check Live health for broker reconciliation."
        : value.state === "startup-refused" ? "Startup was refused. Review your limits, connections and Paper handover."
          : "Startup outcome is unknown. Wait for worker health and inspect the supervisor before retrying; do not repeat login blindly.");
      setStatus(await api("/live/worker"));
    } catch (e) { setNotice(e instanceof Error ? e.message : "Startup could not be confirmed. Check health before retrying."); }
    finally { setBusy(false); }
  }
  async function stop() {
    setBusy(true); setCode(""); setConfirm(false);
    try {
      await api("/live/worker/stop", { confirmation: "STOP ONLY AFTER VERIFIED FLAT" });
      setNotice("Stop requested. The worker will keep managing exposure until a fresh flat broker check permits it to stop.");
      setStatus(await api("/live/worker"));
    } catch (e) { setNotice(e instanceof Error ? e.message : "Stop request failed."); }
    finally { setBusy(false); }
  }
  return <View style={s.card}>
    <Text style={s.title}>Your Live monitoring worker</Text>
    <Text style={s.text}>Start monitoring with your current broker code. This can manage existing Live orders and protection, but cannot place new entries. Paper must already be paused, flat and stopped for this account.</Text>
    <Text style={s.text}>{!status ? "Worker status unavailable." : status.worker_available ? "Worker heartbeat is current. Check Live health for broker status."
      : status.reserved ? "Live slot reserved. No current heartbeat: check the supervisor and broker before authenticating again."
        : "Live monitoring has not started."}</Text>
    {status?.stop_requested && <Text style={s.text}>Waiting for a verified flat book before stopping. If the worker is offline, it needs fresh authentication first; then request stop again.</Text>}
    {notice ? <Text accessibilityRole="alert" style={s.text}>{notice}</Text> : null}
    {reviewed && version && status && !status.worker_available ? <>
      <TextInput accessibilityLabel="Live worker broker TOTP" value={code} onChangeText={setCode}
        editable={!busy} secureTextEntry keyboardType="number-pad" maxLength={6}
        style={s.input} placeholder="Current TOTP" placeholderTextColor="#93a8bd" />
      {!confirm ? <Pressable accessibilityRole="button" disabled={busy || code.length !== 6}
        style={s.button} onPress={() => setConfirm(true)}><Text style={s.label}>Review monitoring startup</Text></Pressable> : <>
        <Text style={s.text}>Confirm startup for your signed-in account. This opens a broker session and may manage existing orders. It will not stop a Paper worker or enable new Live entries.</Text>
        <Pressable accessibilityRole="button" disabled={busy} style={s.button} onPress={start}>
          <Text style={s.label}>Confirm start with entries paused</Text></Pressable>
        <Pressable accessibilityRole="button" onPress={() => { setConfirm(false); setCode(""); }}>
          <Text style={s.text}>Cancel startup</Text></Pressable>
      </>}
    </> : null}
    {status?.reserved && <Pressable accessibilityRole="button" disabled={busy} style={s.button} onPress={stop}>
      <Text style={s.label}>Stop monitoring only when verified flat</Text></Pressable>}
  </View>;
}

const s = StyleSheet.create({
  card: { gap: 12, paddingVertical: 12 },
  title: { color: "#e3edf7", fontSize: 20, fontWeight: "700" },
  text: { color: "#b8cadd", fontSize: 15, lineHeight: 22 },
  input: { borderWidth: 1, borderColor: "#456079", borderRadius: 10, padding: 12, color: "#e3edf7" },
  button: { borderRadius: 12, padding: 14, backgroundColor: "#51dfc4" },
  label: { color: "#062722", textAlign: "center", fontWeight: "700" },
});
