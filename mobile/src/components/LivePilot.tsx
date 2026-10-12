import React, { useEffect, useState } from "react";
import { View, Text, TextInput, Pressable, StyleSheet } from "react-native";
import { api } from "../lib/api";
import LiveControls from "./LiveControls";
import LiveFinance from "./LiveFinance";
import LiveFees from "./LiveFees";
import LiveWorker from "./LiveWorker";
import LiveEntries from "./LiveEntries";

type Policy = {
  policy_complete: boolean; enrolled: boolean; reviewed: boolean; version: number | null;
  limits: Record<string, string | number> | null;
};
type Health = {
  account: { state: string; checked_at: string | null; exposure: number | null;
    uncovered: number | null; pending_commands: number | null;
    incident_count: number | null; unverified_fee_fills: number | null };
};
const healthLabels: Record<string, string> = {
  "not-started": "Live monitoring has not started.", "not-checked": "No broker check recorded.",
  observed: "Last broker book matched.", "awaiting-broker": "Waiting for broker confirmation.",
  stale: "The last broker check is stale.", "review-required": "Operator review is required.",
  "recovery-quarantine": "Recovered data is isolated from trading.",
};
const fields = [
  ["capital", "Capital (₹)"], ["max_order_premium", "Maximum order premium (₹)"],
  ["max_open_premium", "Maximum open premium (₹)"], ["daily_loss", "Daily loss limit (₹)"],
  ["max_trade_loss", "Per-trade risk (₹)"], ["max_open_risk", "Combined open risk (₹)"],
  ["fee_reserve", "Round-trip fee reserve (₹)"], ["max_lots", "Maximum lots"],
  ["max_entries", "Maximum daily entries"],
  ["stop_gap_ticks", "Stop-limit gap (ticks, 1–100)"],
  ["target_timeout_seconds", "Target order timeout (seconds, 1–60)"],
];

const preferenceKeys = ["capital", "max_trade_loss", "daily_loss", "max_lots", "max_entries"];
const simpleFields = preferenceKeys.map((key) => fields.find(([name]) => name === key)!);

export default function LivePilot({ owner, uid, users = [] }: {
  owner: boolean; uid: string; users?: { id: string; username: string; access: string }[];
}) {
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [target, setTarget] = useState(uid);
  const [advanced, setAdvanced] = useState(false);
  const [loadedPreferencesKey, setLoadedPreferencesKey] = useState("");
  const preferencesKey = uid + ":" + target;
  const preferencesLoaded = advanced || loadedPreferencesKey === preferencesKey;
  const [values, setValues] = useState<Record<string, string>>({});
  const [healthResult, setHealth] = useState<{ key: string; value: Health } | null>(null);
  const healthPath = owner ? "/admin/live/health/" + target : "/live/health";
  const healthKey = uid + ":" + healthPath;
  const health = healthResult?.key === healthKey ? healthResult.value : null;
  const [totp, setTotp] = useState("");
  const [funds, setFunds] = useState<{ uid: string; checked_at: string; reported_funds: Record<string, string | null> } | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    let mounted = true;
    api("/live/pilot").then((p) => { if (mounted) setPolicy(p); })
      .catch(() => { if (mounted) setError("Pilot status could not be loaded."); });
    return () => { mounted = false; };
  }, [uid]);
  useEffect(() => {
    let mounted = true;
    async function refresh() {
      try {
        const value = await api(healthPath);
        if (mounted) setHealth({ key: healthKey, value });
      } catch { if (mounted) setHealth(null); }
    }
    void refresh();
    const timer = setInterval(() => { void refresh(); }, 20000);
    return () => { mounted = false; clearInterval(timer); };
  }, [healthPath, healthKey]);
  useEffect(() => {
    let mounted = true;
    if (owner && !advanced) {
      api("/admin/live/risk-preferences/" + target).then((result) => {
        if (mounted) {
          setValues(Object.fromEntries(Object.entries({ ...(result.preferences || {}), ...(result.execution || {}) }).map(([k, v]) => [k, String(v)])));
          setLoadedPreferencesKey(preferencesKey);
        }
      }).catch(() => { if (mounted) setError("Saved Live preferences could not be loaded. Retry before editing."); });
    }
    return () => { mounted = false; };
  }, [owner, target, advanced, preferencesKey]);
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
    <LiveWorker key={"worker-" + uid} uid={uid} version={policy?.version ?? null} reviewed={!!policy?.reviewed} />
    <LiveEntries key={"entries-" + uid} uid={uid} version={policy?.version ?? null} />
    {!owner && <LiveControls key={uid} uid={uid} />}
    <LiveFinance key={"finance-" + uid} uid={uid} />
    <LiveFees key={"fees-" + uid} />
    {error ? <Text accessibilityRole="alert" style={s.error}>{error}</Text> : null}
    {notice ? <Text style={s.text}>{notice}</Text> : null}
    <Text style={s.text}>{policy?.enrolled ? (policy.reviewed ? "Your current limits are reviewed." : "Review your pilot limits below.") : "Your account is not enrolled."}</Text>
    {policy?.enrolled && policy.limits ? <>
      {fields.map(([key, label]) => <Text key={key} style={s.text}>{label}: {policy.limits?.[key]}</Text>)}
      {!policy.policy_complete ? <Text style={s.error}>The owner must save a complete policy including exit limits before you review it.</Text> : button("Review these pilot limits", async () => {
        await api("/live/pilot/review", { version: policy.version, confirmation: "REVIEW PILOT LIMITS" });
        setNotice("Limits reviewed. Real-money activation remains disabled.");
      })}
      {button("Withdraw my pilot enrollment", async () => {
        await api("/live/pilot/revoke", {}); setNotice("Pilot enrollment withdrawn.");
      })}
    </> : null}
    <Text style={s.title}>{owner ? "Selected account’s Live health" : "Your Live health"}</Text>
    <Text style={s.text}>{health ? (healthLabels[health.account.state] || "Status unavailable.") : "Live health is unavailable."}</Text>
    {health?.account.checked_at ? <Text style={s.text}>Last check: {new Date(health.account.checked_at).toLocaleString()}</Text> : null}
    {health?.account.exposure != null ? <Text style={s.text}>Last observed open units: {health.account.exposure}. Units without an observed stop: {health.account.uncovered}.</Text> : null}
    {health?.account.pending_commands ? <Text style={s.error}>Orders awaiting confirmation: {health.account.pending_commands}.</Text> : null}
    {health?.account.incident_count ? <Text accessibilityRole="alert" style={s.error}>Unresolved incidents: {health.account.incident_count}. Review the broker account before further action.</Text> : null}
    {health?.account.unverified_fee_fills ? <Text style={s.text}>Charges are unverified for {health.account.unverified_fee_fills} fills. A new trading budget cannot be calculated yet.</Text> : null}
    {owner || policy?.reviewed ? <>
      <Text style={s.text}>Run the read-only broker check in a planned maintenance window. It refuses an active paper worker and does not stop it. A new broker login may affect existing sessions.</Text>
      <TextInput accessibilityLabel="Current broker TOTP" value={totp} onChangeText={setTotp}
        secureTextEntry keyboardType="number-pad" maxLength={6} style={s.input} placeholder="Current TOTP" placeholderTextColor="#93a8bd" />
      {button("Run read-only broker check", async () => {
        const code = totp; setTotp(""); setFunds(null);
        const result = await api("/live/probe", { totp: code, confirmation: "READ ONLY CHECK" });
        setFunds({ uid, checked_at: result.checked_at, reported_funds: result.reported_funds || {} });
        setNotice(`Read check passed: ${result.order_count} orders, ${result.nonzero_position_count} open positions. Live remains disabled.`);
      })}
      {funds?.uid === uid && <View style={{ gap: 8 }}>
        <Text style={s.title}>Your broker funds snapshot</Text>
        <Text style={s.text}>Checked {new Date(funds.checked_at).toLocaleString()}. Compare this with your Kotak Funds screen. These reported values are not yet verified spendable cash; missing fields remain unknown.</Text>
        {Object.entries(funds.reported_funds).map(([name, value]) =>
          <Text key={name} style={s.text}>{name}: {value === null ? "Not reported" : `₹${value}`}</Text>)}
      </View>}
    </> : null}
    {owner ? <>
      <Text style={s.title}>Your Live risk settings</Text>
      <Text style={s.text}>Select an account and save five risk preferences. These are saved for setup only: they do not replace an active policy, enroll the account or start trading. Paper settings stay unchanged.</Text>
      <Text style={s.text}>Lots and entries are maximums, not targets. A trade may be smaller or skipped when cash or the remaining daily loss budget is insufficient. Real losses can exceed planned limits.</Text>
      <Pressable accessibilityRole="button" disabled={busy} onPress={() => { setAdvanced(!advanced); setValues({}); setLoadedPreferencesKey(""); setNotice(""); setError(""); }}>
        <Text style={s.text}>{advanced ? "Back to five risk preferences" : "Advanced execution policy"}</Text>
      </Pressable>
      {advanced && <Text style={s.text}>This separate complete policy requires all execution settings and account review. It does not automatically use saved preferences. The stop-limit gap does not guarantee a fill; target timeout starts when a target sell is requested.</Text>}
      {users.filter((u) => u.access === "approved").map((u) => <Pressable key={u.id} accessibilityRole="button"
        accessibilityState={{ selected: target === u.id }} disabled={busy}
        onPress={() => { if (target === u.id) return; setTarget(u.id); setValues({}); setLoadedPreferencesKey(""); setNotice(""); setError(""); }} style={s.input}>
        <Text style={s.text}>{target === u.id ? "✓ " : ""}{u.username}</Text>
      </Pressable>)}
      {(advanced ? fields : simpleFields).map(([key, label]) => <View key={key} style={{ gap: 5 }}>
        <Text style={s.text}>{label}</Text>
        <TextInput accessibilityLabel={label} value={values[key] || ""}
          keyboardType="decimal-pad" editable={!busy && preferencesLoaded} style={s.input}
          onChangeText={(value) => setValues((old) => ({ ...old, [key]: value }))} />
      </View>)}
      {button(advanced ? "Save selected account’s execution policy" : "Save five Live risk preferences", async () => {
        if (!preferencesLoaded) throw new Error("Wait for saved preferences to load.");
        if (!advanced) {
          if (simpleFields.some(([key]) => !values[key]?.trim())) throw new Error("Enter all five risk preferences.");
          const limits = Object.fromEntries(preferenceKeys.map((key) => [key,
            key === "max_lots" || key === "max_entries" ? Number(values[key]) : values[key].trim()]));
          await api("/admin/live/risk-preferences/" + target, { limits }, "PUT");
          setNotice("Five preferences saved for setup. They are not applied to trading. Live remains locked; Paper settings are unchanged.");
          return;
        }
        if (fields.some(([key]) => !values[key]?.trim())) throw new Error("Enter every pilot limit.");
        const limits = { ...values, max_lots: Number(values.max_lots), max_entries: Number(values.max_entries),
          stop_gap_ticks: Number(values.stop_gap_ticks), target_timeout_seconds: Number(values.target_timeout_seconds) };
        await api("/admin/live/pilot/" + target, { limits }, "PUT");
        setNotice("Pilot limits saved. The selected account must review them."); setValues({});
      })}
      {!advanced && <>
        <Text style={s.title}>Apply these five values</Text>
        <Text style={s.text}>Order and total open premium caps will equal capital; combined planned open risk will equal the daily loss limit. Cash, fees and remaining loss budget still reduce trade size. Applying creates a new policy that the account must review.</Text>
        <Text style={s.text}>Before the first application, enter the reviewed execution assumptions below. These cannot be inferred from Paper profits or your five risk values. The fee reserve is an estimate per trade; a stop-limit gap does not guarantee an exit fill.</Text>
        {fields.filter(([key]) => ["fee_reserve", "stop_gap_ticks", "target_timeout_seconds"].includes(key)).map(([key, label]) => <View key={key} style={{ gap: 5 }}>
          <Text style={s.text}>{label}</Text>
          <TextInput accessibilityLabel={label} value={values[key] || ""} keyboardType="decimal-pad"
            editable={!busy && preferencesLoaded} style={s.input}
            onChangeText={(value) => setValues((old) => ({ ...old, [key]: value }))} />
        </View>)}
        {button("Apply five values and reviewed execution settings", async () => {
          if (!preferencesLoaded) throw new Error("Wait for saved preferences to load.");
          const keys = [...preferenceKeys, "fee_reserve", "stop_gap_ticks", "target_timeout_seconds"];
          if (keys.some((key) => !values[key]?.trim())) throw new Error("Enter five risk values and the reviewed fee and exit settings.");
          const counts = ["max_lots", "max_entries", "stop_gap_ticks", "target_timeout_seconds"];
          const limits = Object.fromEntries(keys.map((key) => [key, counts.includes(key) ? Number(values[key]) : values[key].trim()]));
          await api("/admin/live/simple-policy/" + target, { limits }, "PUT");
          setNotice("Derived policy saved. The selected account must review it. Live remains locked.");
        })}
      </>}
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
