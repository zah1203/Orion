import React, { useEffect, useState } from "react";
import { View, Text, TextInput, Pressable, StyleSheet } from "react-native";
import { api } from "../lib/api";

type Policy = {
  enrolled: boolean; reviewed: boolean; version: number | null;
  limits: Record<string, string | number> | null;
};
const fields = [
  ["capital", "Capital (₹)"], ["max_order_premium", "Maximum order premium (₹)"],
  ["max_open_premium", "Maximum open premium (₹)"], ["daily_loss", "Daily loss limit (₹)"],
  ["max_trade_loss", "Per-trade risk (₹)"], ["max_open_risk", "Combined open risk (₹)"],
  ["fee_reserve", "Round-trip fee reserve (₹)"], ["max_lots", "Maximum lots"],
  ["max_entries", "Maximum daily entries"],
];

export default function LivePilot({ owner, uid, users = [] }: {
  owner: boolean; uid: string; users?: { id: string; username: string; access: string }[];
}) {
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [target, setTarget] = useState(uid);
  const [values, setValues] = useState<Record<string, string>>({});
  const [totp, setTotp] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    let mounted = true;
    api("/live/pilot").then((p) => { if (mounted) setPolicy(p); })
      .catch(() => { if (mounted) setError("Pilot status could not be loaded."); });
    return () => { mounted = false; };
  }, [uid]);
  async function run(work: () => Promise<void>) {
    setBusy(true); setError(""); setNotice("");
    try { await work(); setPolicy(await api("/live/pilot")); }
    catch (e) { setError(e instanceof Error ? e.message : "Pilot request failed."); }
    finally { setBusy(false); }
  }
  function button(label: string, action: () => Promise<void>) {
    return <Pressable accessibilityRole="button" disabled={busy} style={[s.button, busy && { opacity: 0.4 }]}
      onPress={() => run(action)}><Text style={s.buttonText}>{label}</Text></Pressable>;
  }
  return <View style={s.card}>
    <Text style={s.title}>Live pilot preparation</Text>
    <Text style={s.text}>Live trading is unavailable. Enrollment and limit review do not enable orders.</Text>
    {error ? <Text accessibilityRole="alert" style={s.error}>{error}</Text> : null}
    {notice ? <Text style={s.text}>{notice}</Text> : null}
    <Text style={s.text}>{policy?.enrolled ? (policy.reviewed ? "Your current limits are reviewed." : "Review your pilot limits below.") : "Your account is not enrolled."}</Text>
    {policy?.enrolled && policy.limits ? <>
      {fields.map(([key, label]) => <Text key={key} style={s.text}>{label}: {policy.limits?.[key]}</Text>)}
      {button("Review these pilot limits", async () => {
        await api("/live/pilot/review", { version: policy.version, confirmation: "REVIEW PILOT LIMITS" });
        setNotice("Limits reviewed. Real-money activation remains disabled.");
      })}
      {button("Withdraw my pilot enrollment", async () => {
        await api("/live/pilot/revoke", {}); setNotice("Pilot enrollment withdrawn.");
      })}
    </> : null}
    {owner || policy?.reviewed ? <>
      <Text style={s.text}>Run the read-only broker check in a planned maintenance window. It refuses an active paper worker and does not stop it. A new broker login may affect existing sessions.</Text>
      <TextInput accessibilityLabel="Current broker TOTP" value={totp} onChangeText={setTotp}
        secureTextEntry keyboardType="number-pad" maxLength={6} style={s.input} placeholder="Current TOTP" placeholderTextColor="#93a8bd" />
      {button("Run read-only broker check", async () => {
        const code = totp; setTotp("");
        const result = await api("/live/probe", { totp: code, confirmation: "READ ONLY CHECK" });
        setNotice(`Read check passed: ${result.order_count} orders, ${result.nonzero_position_count} open positions. Live remains disabled.`);
      })}
    </> : null}
    {owner ? <>
      <Text style={s.title}>Configure the two-account pilot</Text>
      <Text style={s.text}>Select an approved account and enter its own limits. Saving requires that account to review again.</Text>
      {users.filter((u) => u.access === "approved").map((u) => <Pressable key={u.id} accessibilityRole="button"
        accessibilityState={{ selected: target === u.id }} disabled={busy}
        onPress={() => { setTarget(u.id); setValues({}); }} style={s.input}>
        <Text style={s.text}>{target === u.id ? "✓ " : ""}{u.username}</Text>
      </Pressable>)}
      {fields.map(([key, label]) => <View key={key} style={{ gap: 5 }}>
        <Text style={s.text}>{label}</Text>
        <TextInput accessibilityLabel={label} value={values[key] || ""}
          keyboardType="decimal-pad" editable={!busy} style={s.input}
          onChangeText={(value) => setValues((old) => ({ ...old, [key]: value }))} />
      </View>)}
      {button("Save selected account’s pilot limits", async () => {
        if (fields.some(([key]) => !values[key]?.trim())) throw new Error("Enter every pilot limit.");
        const limits = { ...values, max_lots: Number(values.max_lots), max_entries: Number(values.max_entries) };
        await api("/admin/live/pilot/" + target, { limits }, "PUT");
        setNotice("Pilot limits saved. The selected account must review them."); setValues({});
      })}
      {button("Revoke selected account’s enrollment", async () => {
        await api("/admin/live/pilot/" + target + "/revoke", {});
        setNotice("Selected account’s pilot enrollment revoked.");
      })}
    </> : null}
  </View>;
}
const s = StyleSheet.create({
  card: { padding: 20, gap: 14, borderRadius: 20, borderWidth: 1, borderColor: "#29445f", backgroundColor: "#102238" },
  title: { color: "#e3edf7", fontSize: 20, fontWeight: "700" },
  text: { color: "#b8cadd", fontSize: 15, lineHeight: 22 },
  error: { color: "#ff889a", fontSize: 15 },
  input: { borderWidth: 1, borderColor: "#456079", borderRadius: 10, padding: 12, color: "#e3edf7" },
  button: { borderRadius: 12, padding: 14, backgroundColor: "#51dfc4" },
  buttonText: { color: "#062722", textAlign: "center", fontWeight: "700" },
});
