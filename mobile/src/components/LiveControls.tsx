import React, { useEffect, useState } from "react";
import { View, Text, TextInput, Pressable, StyleSheet } from "react-native";
import { api } from "../lib/api";

type Position = { tag: string; request_id: string; symbol: string; status: string;
  remaining: number; entry_quantity: number; tick: string; can_cancel: boolean; can_exit: boolean };
type Snapshot = { worker_available: boolean; entries_paused: boolean; positions: Position[];
  requests: { request_id: string; entry_tag: string; action: string; state: string }[] };
type Confirmation = { row: Position; action: "CANCEL" | "EXIT"; limit: string };

export default function LiveControls({ uid }: { uid: string }) {
  const [data, setData] = useState<Snapshot | null>(null);
  const [confirm, setConfirm] = useState<Confirmation | null>(null);
  const [price, setPrice] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    let mounted = true;
    const refresh = async () => {
      try { const value = await api("/live/controls"); if (mounted) setData(value); }
      catch { if (mounted) { setData(null); setError("Live controls could not be refreshed."); } }
    };
    void refresh();
    const timer = setInterval(() => { void refresh(); }, 4000);
    return () => { mounted = false; clearInterval(timer); };
  }, [uid]);
  async function submit() {
    if (!confirm || busy) return;
    setBusy(true); setError("");
    try {
      const result = await api("/live/controls", { request_id: confirm.row.request_id,
        entry_tag: confirm.row.tag, action: confirm.action,
        limit_price: confirm.action === "EXIT" ? confirm.limit : null,
        confirmation: "CONFIRM LIVE ACTION" });
      setNotice(result.state === "DONE" ? "Request completed." : "Request recorded. Waiting for the worker and broker confirmation.");
      setConfirm(null);
      setData(await api("/live/controls"));
    } catch (e) { setError(e instanceof Error ? e.message : "Request outcome unknown. Check its status before retrying."); }
    finally { setBusy(false); }
  }
  async function pause() {
    setBusy(true); setError("");
    try { await api("/live/pause", {}); setNotice("New entries paused. Existing positions remain managed."); setData(await api("/live/controls")); }
    catch (e) { setError(e instanceof Error ? e.message : "Pause could not be confirmed."); }
    finally { setBusy(false); }
  }
  const button = (label: string, onPress: () => void, disabled = false) => (
    <Pressable accessibilityRole="button" disabled={busy || disabled} onPress={onPress}
      style={[s.button, (busy || disabled) && { opacity: 0.4 }]}><Text style={s.text}>{label}</Text></Pressable>
  );
  return <View style={s.card}>
    <Text style={s.title}>Your Live trade controls</Text>
    <Text style={s.text}>{data?.worker_available ? "Connected to your Live worker." : "No active Live worker. Controls cannot start trading."}</Text>
    {error ? <Text accessibilityRole="alert" style={s.error}>{error}</Text> : null}
    {notice ? <Text style={s.text}>{notice}</Text> : null}
    {data?.worker_available && button("Pause new Live entries", () => { void pause(); }, data.entries_paused)}
    {data?.positions.map(row => <View key={row.tag} style={s.position}>
      <Text style={s.title}>{row.symbol}</Text>
      <Text style={s.text}>{row.remaining} units open · Entry {row.status.toLowerCase()}</Text>
      {row.can_cancel && button("Cancel pending entry", () => setConfirm({ row, action: "CANCEL", limit: "" }))}
      {row.can_exit && <>
        <Text style={s.text}>Minimum sell price per unit (₹), tick {row.tick}. A limit exit may not fill.</Text>
        <TextInput accessibilityLabel={`Exit limit for ${row.symbol}`} keyboardType="decimal-pad"
          value={price[row.tag] || ""} onChangeText={value => setPrice(old => ({ ...old, [row.tag]: value }))}
          style={s.input} />
        {button("Review exit position", () => setConfirm({ row, action: "EXIT", limit: price[row.tag] || "" }), !price[row.tag])}
      </>}
    </View>)}
    {confirm && <View style={s.position}>
      <Text style={s.title}>Confirm for your Live account</Text>
      <Text style={s.text}>{confirm.row.symbol}</Text>
      <Text style={s.text}>{confirm.action === "EXIT"
        ? `Exit up to ${confirm.row.remaining} open units at ₹${confirm.limit} or better. Existing sells must be confirmed cancelled first. The request expires after 60 seconds; it is not a guaranteed exit.`
        : "Cancel the unfilled entry quantity. Any fills remain open and protected."}</Text>
      {button(confirm.action === "EXIT" ? "Confirm Live exit" : "Confirm Live cancellation", () => { void submit(); }, !data?.worker_available)}
      {button("Back", () => setConfirm(null))}
    </View>}
    {data?.requests.slice(0, 5).map(item => <Text key={item.request_id} style={s.text}>
      {item.action === "EXIT" ? "Exit" : "Cancellation"}: {item.state === "PENDING" ? "pending broker confirmation" : item.state === "DONE" ? "confirmed" : "stopped — review required"}
    </Text>)}
  </View>;
}

const s = StyleSheet.create({
  card: { marginTop: 20, gap: 12 }, position: { gap: 10, paddingVertical: 12, borderTopWidth: 1, borderColor: "#334960" },
  title: { color: "#e5edf8", fontSize: 18, fontWeight: "700" }, text: { color: "#c6d5e7", fontSize: 15 },
  error: { color: "#ffbb74", fontSize: 15 }, button: { padding: 14, borderRadius: 12, backgroundColor: "#20394d" },
  input: { color: "#e5edf8", borderWidth: 1, borderColor: "#536e88", borderRadius: 12, padding: 12 },
});
