import React, { useEffect, useState } from "react";
import { View, Text, TextInput, Pressable, StyleSheet } from "react-native";
import { api } from "../lib/api";

type Review = { day: string; opening_cash: string; reviewed_at: string };
const confirmation = "DEPOSITED CASH ONLY; NO EXTERNAL TRADES OR WITHDRAWALS; FEES ARE ESTIMATES";

export default function LiveFinance({ uid }: { uid: string }) {
  const [cash, setCash] = useState("");
  const [review, setReview] = useState<Review | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    let mounted = true;
    api("/live/finance").then((data) => { if (mounted) setReview(data.latest_cash_review); })
      .catch(() => { if (mounted) setError("Cash review could not be loaded."); });
    return () => { mounted = false; };
  }, [uid]);
  async function submit() {
    if (busy || !confirmed) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await api("/live/finance/cash", { opening_cash: cash.trim(), confirmation });
      setReview((await api("/live/finance")).latest_cash_review);
      setNotice("Daily allocation recorded. Entries remain paused and Live activation stays disabled.");
      setCash(""); setConfirmed(false);
    } catch (e) { setError(e instanceof Error ? e.message : "Cash review was not confirmed. Refresh before retrying."); }
    finally { setBusy(false); }
  }
  return <View style={s.card}>
    <Text style={s.title}>Your daily Live cash allocation</Text>
    <Text style={s.text}>Choose an amount no higher than your settled deposited cash and reviewed capital limit. Do not include collateral, unsettled sale proceeds or credit. This is a spending cap, not a broker balance check.</Text>
    <Text style={s.text}>Review before the first trade, with a flat account and fresh Live monitoring. No same-day increase or reset is allowed. Sales do not refill this allocation. Previous days’ charges must be reconciled first.</Text>
    {review && <Text style={s.text}>Last recorded allocation: ₹{review.opening_cash} for {review.day} (India time). This record alone does not confirm today’s eligibility.</Text>}
    <TextInput accessibilityLabel="Opening deposited cash allocation in rupees" value={cash}
      onChangeText={(value) => { setCash(value); setConfirmed(false); }} keyboardType="decimal-pad"
      maxLength={30} editable={!busy} style={s.input} placeholder="Opening allocation (₹)" placeholderTextColor="#93a8bd" />
    <Pressable accessibilityRole="checkbox" accessibilityState={{ checked: confirmed }} disabled={busy}
      onPress={() => setConfirmed(!confirmed)} style={s.input}>
      <Text style={s.text}>{confirmed ? "✓ " : "○ "}I reviewed this amount against my broker records. I use deposited cash only, will make no outside trades or withdrawals while Orion runs, and accept the reviewed fee reserve as an estimate. Actual charges can be higher.</Text>
    </Pressable>
    <Pressable accessibilityRole="button" disabled={busy || !confirmed || !cash.trim()}
      onPress={() => { void submit(); }} style={[s.button, (busy || !confirmed || !cash.trim()) && { opacity: 0.4 }]}>
      <Text style={s.buttonText}>{busy ? "Recording review…" : "Record my daily cash review"}</Text>
    </Pressable>
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
  button: { borderRadius: 12, padding: 14, backgroundColor: "#51dfc4" },
  buttonText: { color: "#062722", textAlign: "center", fontWeight: "700" },
});
