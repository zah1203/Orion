import React, { useEffect, useState } from "react";
import { View, Text, Pressable, ScrollView, StyleSheet } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { router, useLocalSearchParams } from "expo-router";
import { api, restore } from "../lib/api";

type Data = Record<string, any>;
const money = (v: unknown) => v == null ? "Not recorded" : "₹" + Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const time = (v: string) => v ? new Date(v).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" }) + " IST" : "Not recorded";
const labels: Record<string, string> = {
  PAPER_BUY: "Entry filled", TARGET_REACHED: "Target reached", PAPER_EXIT: "Position closed",
  PROVIDER_EXIT_PENDING_QUOTE: "Provider close accepted — waiting for a quote",
  ENTRY_LIMIT_REJECTED: "Entry blocked", REVIEW_OR_COMMENTARY: "Message not executed",
  EXIT_REQUIRES_REVIEW: "Close instruction needs review", SIGNAL_PENDING: "Waiting for entry",
  PENDING_CANCELLED_BY_PROVIDER: "Pending entry cancelled by provider",
};
function Action({ label, onPress }: { label: string; onPress: () => void }) {
  return <Pressable accessibilityRole="button" onPress={onPress} style={s.button}><Text style={s.link}>{label}</Text></Pressable>;
}
export default function Trade() {
  const params = useLocalSearchParams<{ signal: string; userId?: string }>();
  const signal = typeof params.signal === "string" ? params.signal : "";
  const userId = typeof params.userId === "string" ? params.userId : "";
  const key = JSON.stringify([signal, userId]);
  const [result, setResult] = useState<{ key: string; requestKey: string; data: Data } | null>(null);
  const [offset, setOffset] = useState(0), [refresh, setRefresh] = useState(0);
  const requestKey = JSON.stringify([key, offset, refresh]);
  const [status, setStatus] = useState<{ key: string; error: string } | null>(null);
  const busy = status?.key !== requestKey;
  const error = status?.key === requestKey ? status.error : "";
  useEffect(() => {
    let valid = true;
    const query = new URLSearchParams({ signal_id: signal, offset: String(offset) });
    restore().then(() => api((userId ? "/admin/users/" + encodeURIComponent(userId) : "") + "/paper-trade?" + query))
      .then((data) => {
        if (valid) {
          setResult((old) => ({ key, requestKey, data: { ...data, timeline: offset && old?.key === key ? [...old.data.timeline.slice(0, offset), ...data.timeline] : data.timeline } }));
          setStatus({ key: requestKey, error: "" });
        }
      })
      .catch((e) => { if (valid) setStatus({ key: requestKey, error: e.message || "Could not load trade details" }); });
    return () => { valid = false; };
  }, [signal, userId, key, offset, requestKey]);
  const data = result?.key === key && (offset > 0 || result.requestKey === requestKey) ? result.data : null;
  return <SafeAreaView style={s.page}><ScrollView contentContainerStyle={s.content}>
    <View style={s.row}>
      <Action label="‹ Back" onPress={() => router.canGoBack() ? router.back() : router.replace("/")} />
      <Action label="Refresh" onPress={() => { setOffset(0); setRefresh((n) => n + 1); }} />
    </View>
    <Text style={s.eyebrow}>PAPER TRADE · EXECUTION & TELEGRAM</Text>
    <Text style={s.title}>{data?.symbol || "Trade details"}</Text>
    {!!error && <View style={s.card}><Text style={s.loss}>{error}</Text><Action label="Retry" onPress={() => setRefresh((n) => n + 1)} /></View>}
    {busy && <Text style={s.note}>Loading recorded evidence…</Text>}
    {data && <>
      <Text selectable style={s.note}>{data.channel_name} · {data.channel_id}</Text>
      <Text style={s.note}>Channel name: {data.channel_name_source} · Signal {data.signal_id}</Text>
      <View style={s.card}>
        <Text style={s.heading}>{data.outcome} · {data.status}</Text>
        <Text style={[s.amount, Number(data.realized_pnl) < 0 && s.loss]}>{money(data.realized_pnl)}</Text>
        <Text style={s.note}>Cumulative realized P&L after recorded fees. A stop exit can still finish in profit.</Text>
        {[
          ["Actual entry", money(data.entry_price)], ["Entry time", time(data.entry_time)],
          ["Initial stop", money(data.initial_stop)], ["Latest stop", money(data.current_stop)],
          ["Final exit", data.exit_reason ? `${data.exit_reason} · ${money(data.exit_price)}` : "No recorded final exit"],
          ["Lots", `${data.lots} entered · ${data.remaining} remaining`],
        ].map(([label, value]) => <View key={label} style={s.row}><Text style={s.note}>{label}</Text><Text style={s.value}>{value}</Text></View>)}
        <Text style={s.text}>{data.targets.map((t: string, i: number) => `T${i + 1} ${money(t)}`).join("   ·   ") || "Targets not recorded"}</Text>
      </View>
      <Text style={s.heading}>Compare the timeline</Text>
      <Text style={s.note}>Ordered by when Orion recorded each event. Telegram messages also show the provider timestamp. Reported target hits are not proof of an Orion fill.</Text>
      {data.timeline.map((item: Data, index: number) => <View key={`${item.kind}-${item.seq || item.message_id}-${index}`} style={[s.card, item.kind === "telegram" && s.telegram]}>
        <Text style={s.eyebrow}>{item.kind === "telegram" ? "TELEGRAM" : "ORION"} · {time(item.at)}</Text>
        {item.kind === "telegram" ? <>
          <Text style={s.note}>Message {item.message_id}{item.reply_to !== "None" ? ` · Reply to ${item.reply_to}` : ""}{item.edited ? " · Edited version" : ""}</Text>
          <Text style={s.note}>Provider time: {time(item.source_time)}</Text>
          <Text selectable style={s.text}>{item.text || "No text retained — this may be a media-only message."}</Text>
        </> : <>
          <Text style={s.heading}>{labels[item.event] || item.event.replaceAll("_", " ")}{item.event === "TARGET_REACHED" ? ` · T${item.body.target}` : ""}</Text>
          {item.body.reason && <Text style={s.text}>Reason: {item.body.reason}</Text>}
          {item.body.price != null && <Text style={s.text}>Executed price: {money(item.body.price)}</Text>}
          {item.body.stop != null && <Text style={s.text}>Stop: {money(item.body.stop)}</Text>}
          {item.body.lots != null && <Text style={s.text}>Entered lots: {item.body.lots}</Text>}
          {item.body.sold_lots != null && <Text style={s.text}>Sold {item.body.sold_lots} lots · {item.body.remaining} remaining</Text>}
          {item.body.pnl != null && <Text style={s.text}>Cumulative trade P&L: {money(item.body.pnl)}</Text>}
          {item.body.realized_delta != null && <Text style={s.note}>This event’s P&L change: {money(item.body.realized_delta)} · Fee: {money(item.body.fee)}</Text>}
          {item.body.blockers?.map((b: string) => <Text key={b} style={s.loss}>{b}</Text>)}
          {item.event === "ENTRY_LIMIT_REJECTED" && !item.body.blockers && <Text style={s.note}>Exact rejection reason was not recorded in this older event.</Text>}
          {item.body.observed && <Text style={s.note}>At decision: bid {money(item.body.observed.bid)}, ask {money(item.body.observed.ask)}, spread {(Number(item.body.observed.spread_fraction)*100).toFixed(2)}%; {item.body.observed.open_positions} open positions; {item.body.observed.entries} entries; daily P&L {money(item.body.observed.daily_pnl)}.</Text>}
          {item.body.limits && <Text style={s.note}>Limits then: spread {(Number(item.body.limits.max_spread_fraction)*100).toFixed(2)}%; {item.body.limits.max_open_positions} open positions; {item.body.limits.max_entries_per_day} entries; daily loss {money(item.body.limits.daily_loss_limit)}.</Text>}
        </>}
      </View>)}
      {data.next_offset != null && !busy && <Action label="Load more timeline" onPress={() => setOffset(data.next_offset)} />}
      {data.notes.map((note: string) => <Text key={note} style={s.note}>{note}</Text>)}
    </>}
  </ScrollView></SafeAreaView>;
}
const s = StyleSheet.create({
  page: { flex: 1, backgroundColor: "#07111f" }, content: { padding: 20, gap: 14, maxWidth: 880, width: "100%", alignSelf: "center", paddingBottom: 60 },
  row: { flexDirection: "row", flexWrap: "wrap", justifyContent: "space-between", gap: 10 },
  card: { backgroundColor: "#102238", borderColor: "#284259", borderWidth: 1, borderRadius: 16, padding: 18, gap: 10 },
  telegram: { backgroundColor: "#102c37", borderColor: "#286074" },
  title: { color: "#fff", fontSize: 25, fontWeight: "700" }, heading: { color: "#e1eefb", fontSize: 17, fontWeight: "600" },
  eyebrow: { color: "#83cddd", fontSize: 11, fontWeight: "700", letterSpacing: 1 },
  text: { color: "#e1eefb", fontSize: 15, lineHeight: 23 }, note: { color: "#a5bbce", fontSize: 13, lineHeight: 20 },
  value: { color: "#e1eefb", fontSize: 14, flexShrink: 1 }, amount: { color: "#80e1bc", fontSize: 30, fontWeight: "700" },
  loss: { color: "#ff98a6" }, button: { paddingVertical: 12, paddingHorizontal: 8 }, link: { color: "#80e1bc", fontWeight: "600", fontSize: 15 },
});
