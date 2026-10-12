import React, { useState } from "react";
import { Text, View } from "react-native";
import { api } from "../lib/api";
import { Action, Panel } from "./Desk";

const money = (value: unknown) => value == null ? "Unavailable" :
  "₹" + Number(value).toLocaleString("en-IN", { maximumFractionDigits: 2 });

export default function Capital({ uid }: { uid?: string }) {
  const [report, setReport] = useState<Record<string, any> | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [checked, setChecked] = useState("");
  async function check() {
    setBusy(true); setError(""); setReport(null);
    try {
      const value = await api(uid ? `/admin/users/${uid}/paper-capital` : "/paper-capital");
      setReport(value); setChecked(new Date().toLocaleString());
    } catch {
      setError("Could not reconcile this account. Its balance and history have not been changed.");
    } finally { setBusy(false); }
  }
  const text = { color: "#d6e5f3", fontSize: 15, lineHeight: 23 };
  return <Panel>
    <Text style={{ ...text, fontSize: 20, fontWeight: "700" }}>Paper balance explained</Text>
    <Text style={text}>Setting cash to ₹3 lakh preserves earlier profits in history. It does not add those profits on top of ₹3 lakh.</Text>
    <Action label={busy ? "Checking…" : report ? "Refresh balance check" : "Check balance and history"}
      disabled={busy} onPress={check} />
    {error ? <Text accessibilityRole="alert" style={text}>{error}</Text> : null}
    {report && <View style={{ gap: 8 }}>
      <Text style={text}>Snapshot checked: {checked}</Text>
      {report.status === "not-started" ? <Text style={text}>No Paper ledger yet.</Text> : <>
        <Text style={{ ...text, color: report.status === "consistent" ? "#4af0ce" : "#ffbc78" }}>
          {report.status === "consistent" ? "Retained records agree" : "Accounting review required"}
        </Text>
        {[
          ["Configured starting cash", "starting_cash"],
          ["Net balance adjustments", "net_adjustments"],
          ["Net contributed capital", "net_contributed_capital"],
          ["Historical realized P&L", "realized_pnl"],
          ["Remaining position cost", "open_cost_basis"],
          ["Expected available cash", "expected_cash"],
          ["Recorded available cash", "actual_cash"],
          ["Unexplained difference", "unexplained_difference"],
        ].map(([label, key]) => <Text key={key} style={text}>{label}: {money(report[key])}</Text>)}
        {report.adjustments.length > 0 && <Text style={text}>Recorded balance changes</Text>}
        {report.adjustments.map((item: Record<string, any>) => <Text key={item.seq} style={text}>
          {new Date(item.at).toLocaleString()}: {money(item.before)} → {money(item.after)} ({money(item.adjustment)})
        </Text>)}
        {report.issues.length > 0 && <Text style={text}>Checks requiring review: {report.issues.join(", ")}</Text>}
        <Text style={text}>{report.note}</Text>
        <Text style={text}>This snapshot does not update automatically. No balances or trades were changed. Live trading remains locked.</Text>
      </>}
    </View>}
  </Panel>;
}
