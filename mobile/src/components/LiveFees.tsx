import React, { useState } from "react";
import { View, Text, TextInput, Pressable, StyleSheet } from "react-native";
import { api } from "../lib/api";

type Statement = { account: string; ucc: string; trade_day: string; supersedes: string | null;
  charges: Record<string, string>; fills: { segment: string; trade_id: string; broker_id: string;
    side: string; quantity: number; price: string; total_fee: string }[] };
const labels: Record<string, string> = { brokerage: "Brokerage", exchange: "Exchange charges",
  sebi: "SEBI charges", stt: "Securities transaction tax", stamp: "Stamp duty", gst: "GST", other: "Other charges" };

export default function LiveFees() {
  const [day, setDay] = useState("");
  const [statement, setStatement] = useState<Statement | null>(null);
  const [proof, setProof] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  async function load() {
    setBusy(true); setError(""); setNotice(""); setStatement(null); setProof(""); setConfirmed(false);
    try { setStatement(await api("/live/finance/statement/" + encodeURIComponent(day.trim()))); }
    catch (e) { setError(e instanceof Error ? e.message : "Statement trades could not be loaded."); }
    finally { setBusy(false); }
  }
  async function submit() {
    if (!statement || !confirmed || busy) return;
    setBusy(true); setError(""); setNotice("");
    const source = proof; setProof(""); setConfirmed(false);
    try {
      const result = await api("/live/finance/statement", { statement, proof_text: source,
        confirmation: "I REVIEWED THE BROKER STATEMENT" });
      setNotice(result.duplicate ? "This review is already recorded. Later corrections may still require review." : "Statement review recorded. Entries remain paused.");
      setStatement(null);
    } catch (e) { setError(e instanceof Error ? e.message : "Review outcome unknown. Reload the day before retrying."); }
    finally { setBusy(false); }
  }
  function button(label: string, action: () => void, disabled = false) {
    return <Pressable accessibilityRole="button" onPress={action} disabled={busy || disabled}
      style={[s.button, (busy || disabled) && { opacity: 0.4 }]}><Text style={s.text}>{label}</Text></Pressable>;
  }
  return <View style={s.card}>
    <Text style={s.title}>Review previous-day Live charges</Text>
    <Text style={s.text}>Use your final broker contract note after the trading day has closed. Match every trade and enter every charge, including zero where confirmed. This is a manual review; Orion does not verify or parse the document automatically.</Text>
    <TextInput accessibilityLabel="Statement trading day YYYY-MM-DD" value={day} editable={!busy}
      onChangeText={(value) => { setDay(value); setStatement(null); setProof(""); setConfirmed(false); }}
      placeholder="YYYY-MM-DD (India trading day)" placeholderTextColor="#93a8bd" maxLength={10} style={s.input} />
    {button("Load my trades for this day", () => { void load(); }, !/^\d{4}-\d{2}-\d{2}$/.test(day))}
    {statement && <>
      {statement.supersedes ? <Text style={s.text}>This will append a new review of this day. Earlier evidence is retained.</Text> : null}
      {Object.keys(statement.charges).map((key) => <View key={key}>
        <Text style={s.text}>{labels[key]} (₹)</Text>
        <TextInput accessibilityLabel={labels[key]} keyboardType="decimal-pad" maxLength={30} editable={!busy}
          value={statement.charges[key]} style={s.input} onChangeText={(value) => {
            setConfirmed(false); setStatement({ ...statement, charges: { ...statement.charges, [key]: value } });
          }} />
      </View>)}
      <Text style={s.text}>Allocate the statement’s full charge total across these fills to the paise. The allocations must add up exactly. If any broker trade is absent, do not confirm; request reconciliation.</Text>
      {statement.fills.map((fill, index) => <View key={fill.segment + ":" + fill.trade_id}>
        <Text style={s.text}>{fill.side} {fill.quantity} units at ₹{fill.price} · Trade {fill.trade_id} · Order {fill.broker_id}</Text>
        <TextInput accessibilityLabel={"Allocated charges for trade " + fill.trade_id} keyboardType="decimal-pad"
          maxLength={30} editable={!busy} value={fill.total_fee} style={s.input}
          onChangeText={(value) => { setConfirmed(false); setStatement({ ...statement,
            fills: statement.fills.map((f, i) => i === index ? { ...f, total_fee: value } : f) }); }} />
      </View>)}
      <Text style={s.text}>Paste the original contract-note text below for its review fingerprint. Keep the original document privately for audit. The text is not saved on the server.</Text>
      <TextInput accessibilityLabel="Original contract note text" value={proof} editable={!busy} multiline
        maxLength={100000} style={[s.input, { minHeight: 100 }]} autoCorrect={false}
        onChangeText={(value) => { setProof(value); setConfirmed(false); }} />
      <Pressable accessibilityRole="checkbox" accessibilityState={{ checked: confirmed }} disabled={busy}
        onPress={() => setConfirmed(!confirmed)} style={s.input}>
        <Text style={s.text}>{confirmed ? "✓ " : "○ "}I checked my account, the complete day’s trades and the full charges against the final broker statement.</Text>
      </Pressable>
      {button("Record my statement review", () => { void submit(); }, !confirmed || !proof.trim())}
    </>}
    {notice ? <Text style={s.text}>{notice}</Text> : null}
    {error ? <Text accessibilityRole="alert" style={s.error}>{error}</Text> : null}
  </View>;
}
const s = StyleSheet.create({
  card: { gap: 12, paddingVertical: 12 },
  title: { color: "#e3edf7", fontSize: 20, fontWeight: "700" },
  text: { color: "#b8cadd", fontSize: 15, lineHeight: 22 },
  error: { color: "#ff889a", fontSize: 15 },
  input: { borderWidth: 1, borderColor: "#456079", borderRadius: 10, padding: 12, color: "#e3edf7" },
  button: { borderWidth: 1, borderColor: "#51dfc4", borderRadius: 12, padding: 14 },
});
