import React, { useEffect, useState } from "react";
import {
  View,
  Text,
  Pressable,
  ScrollView,
  TextInput,
  StyleSheet,
  AppState,
} from "react-native";
import { api } from "../lib/api";
type Data = Record<string, any>;
const money = (v: unknown) =>
  v === null || v === undefined
    ? "Unavailable"
    : "₹" + Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const products = [
  "ALL",
  "NIFTY",
  "BANKNIFTY",
  "CRUDEOIL",
  "CRUDEOILM",
  "GOLD",
  "GOLDM",
  "SILVER",
  "SILVERM",
];
function Chip({
  label,
  active,
  onPress,
}: {
  label: string;
  active: boolean;
  onPress: () => void;
}) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ selected: active }}
      onPress={onPress}
      style={[s.chip, active && s.active]}
    >
      <Text style={[s.chipText, active && { color: "#092c25" }]}>{label}</Text>
    </Pressable>
  );
}
export default function Performance({ userId }: { userId?: string }) {
  const [period, setPeriod] = useState("week"),
    [instrument, setInstrument] = useState("ALL"),
    [group, setGroup] = useState("day");
  const [start, setStart] = useState(""),
    [end, setEnd] = useState(""),
    [range, setRange] = useState({ start: "", end: "" });
  const [result, setResult] = useState<{
    key: string;
    data: Data | null;
    error: string;
  } | null>(null);
  const [error, setError] = useState(""),
    [selected, setSelected] = useState<string | null>(null),
    [refresh, setRefresh] = useState(0),
    [showTrades, setShowTrades] = useState(false);
  const ready = period !== "custom" || Boolean(range.start && range.end);
  const requestKey = JSON.stringify([
    period,
    instrument,
    group,
    range,
    userId,
    refresh,
  ]);
  const current = result?.key === requestKey ? result : null;
  const data = current?.data;
  const busy = ready && !current;
  useEffect(() => {
    const t = setInterval(() => {
      if (AppState.currentState === "active") setRefresh((n) => n + 1);
    }, 30000);
    return () => clearInterval(t);
  }, []);
  useEffect(() => {
    if (!ready) return;
    let valid = true;
    const q = new URLSearchParams({
      period,
      instrument,
      group_by: group,
      ...(period === "custom" ? range : {}),
    });
    api(
      (userId ? "/admin/users/" + userId : "") + "/performance?" + q.toString(),
    )
      .then((data) => {
        if (valid) setResult({ key: requestKey, data, error: "" });
      })
      .catch((e) => {
        if (valid) setResult({ key: requestKey, data: null, error: e.message });
      });
    return () => {
      valid = false;
    };
  }, [period, instrument, group, range, userId, ready, requestKey]);
  const chart = data?.chart?.slice(-60) || [];
  const scale = Math.max(
    1,
    ...chart.map((x: Data) => Math.abs(Number(x.realized || 0))),
  );
  const detail = chart.find((x: Data) => x.date === selected);
  return (
    <View style={s.panel}>
      <Text style={s.title}>Explore performance</Text>
      <Text style={s.note}>
        Paper trading · Realized P&L after simulation fees
      </Text>
      <View style={s.wrap}>
        {[
          ["today", "Today"],
          ["week", "This week"],
          ["month", "This month"],
          ["all", "All time"],
          ["custom", "Custom"],
        ].map(([v, label]) => (
          <Chip
            key={v}
            label={label}
            active={period === v}
            onPress={() => {
              setError("");
              setPeriod(v);
              if (v === "all") setGroup("month");
              else setGroup("day");
            }}
          />
        ))}
      </View>
      {period === "custom" && (
        <View style={{ gap: 10 }}>
          <Text style={s.note}>Inclusive dates in India · YYYY-MM-DD</Text>
          <TextInput
            accessibilityLabel="Performance start date"
            placeholder="Start: YYYY-MM-DD"
            placeholderTextColor="#849ab5"
            value={start}
            onChangeText={setStart}
            style={s.input}
          />
          <TextInput
            accessibilityLabel="Performance end date"
            placeholder="End: YYYY-MM-DD"
            placeholderTextColor="#849ab5"
            value={end}
            onChangeText={setEnd}
            style={s.input}
          />
          <Chip
            label="Apply dates"
            active={false}
            onPress={() => {
              if (
                !/^\d{4}-\d{2}-\d{2}$/.test(start) ||
                !/^\d{4}-\d{2}-\d{2}$/.test(end)
              ) {
                setError("Use YYYY-MM-DD for both dates");
                return;
              }
              setError("");
              setRange({ start, end });
            }}
          />
        </View>
      )}
      <Text style={s.label}>Instrument</Text>
      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        contentContainerStyle={{ gap: 8 }}
      >
        {products.map((p) => (
          <Chip
            key={p}
            label={p === "ALL" ? "All instruments" : p}
            active={instrument === p}
            onPress={() => setInstrument(p)}
          />
        ))}
      </ScrollView>
      {(error || current?.error) && (
        <Text accessibilityRole="alert" style={s.error}>
          {error || current?.error}
        </Text>
      )}
      {busy && <Text style={s.note}>Updating performance…</Text>}
      {data && (
        <>
          <Text style={s.note}>
            {data.start} — {data.end} · IST ·{" "}
            {instrument === "ALL" ? "All instruments" : instrument}
          </Text>
          <Text style={s.label}>Realized in selected period</Text>
          <Text style={[s.total, Number(data.realized) < 0 && s.loss]}>
            {money(data.realized)}
          </Text>
          <Text style={s.note}>
            {data.entries} {data.entries === 1 ? "entry" : "entries"} filled in
            this period
          </Text>
          {data.incomplete_breakdown && (
            <Text style={s.warning}>
              Some older trades span multiple dates without a recorded fee
              breakdown. Unavailable values are not treated as zero. Broader
              periods may have exact totals.
            </Text>
          )}
          <Text style={s.label}>Group chart by</Text>
          <View style={s.wrap}>
            {[
              ["day", "Day"],
              ["week", "Week"],
              ["month", "Month"],
            ].map(([v, l]) => (
              <Chip
                key={v}
                label={l}
                active={group === v}
                onPress={() => setGroup(v)}
              />
            ))}
          </View>
          <Text style={s.note}>
            Tap a bar for its P&L. Green = profit; pink = loss.
            {data.chart.length > 60
              ? " Showing the latest 60 buckets; summary includes the full range."
              : ""}
          </Text>
          <ScrollView
            horizontal
            contentContainerStyle={{ gap: 8, paddingVertical: 12 }}
          >
            {chart.map((x: Data) => (
              <Pressable
                key={x.date}
                accessibilityRole="button"
                accessibilityLabel={`${x.date}: ${money(x.realized)}`}
                onPress={() => setSelected(x.date)}
                style={[
                  s.barColumn,
                  selected === x.date && { backgroundColor: "#243e55" },
                ]}
              >
                <View style={{ height: 62, justifyContent: "flex-end" }}>
                  {x.realized !== null && Number(x.realized) > 0 && (
                    <View
                      style={[
                        s.bar,
                        {
                          height: Math.max(
                            3,
                            (Math.abs(Number(x.realized)) / scale) * 60,
                          ),
                        },
                      ]}
                    />
                  )}
                </View>
                <View style={s.zero} />
                <View style={{ height: 62 }}>
                  {x.realized !== null && Number(x.realized) < 0 && (
                    <View
                      style={[
                        s.bar,
                        {
                          height: Math.max(
                            3,
                            (Math.abs(Number(x.realized)) / scale) * 60,
                          ),
                          backgroundColor: "#ff889a",
                        },
                      ]}
                    />
                  )}
                  {x.realized === null && <Text style={s.warning}>N/A</Text>}
                  {x.realized !== null && Number(x.realized) === 0 && (
                    <Text style={s.note}>0</Text>
                  )}
                </View>
                <Text style={s.axis}>
                  {group === "month" ? x.date.slice(0, 7) : x.date.slice(5)}
                </Text>
              </Pressable>
            ))}
          </ScrollView>
          {detail && (
            <View style={s.detail}>
              <Text style={s.text}>
                {group === "week"
                  ? "Week of "
                  : group === "month"
                    ? "Month of "
                    : ""}
                {detail.date}
              </Text>
              <Text style={s.text}>
                {money(detail.realized)} · {detail.entries} entries
              </Text>
              <Text style={s.note}>
                Only dates inside the selected range are included.
              </Text>
            </View>
          )}
          <Text style={s.label}>P&L by instrument · selected period</Text>
          {data.instruments.length === 0 && (
            <Text style={s.note}>No realized activity in this selection.</Text>
          )}
          {data.instruments.map((x: Data) => (
            <Pressable
              key={x.product}
              accessibilityRole="button"
              accessibilityLabel={`Filter ${x.product}`}
              onPress={() => setInstrument(x.product)}
              style={s.row}
            >
              <View>
                <Text style={s.text}>{x.product}</Text>
                <Text style={s.note}>{x.entries} entries</Text>
              </View>
              <Text style={[s.amount, Number(x.realized) < 0 && s.loss]}>
                {money(x.realized)} →
              </Text>
            </Pressable>
          ))}
          <View style={s.detail}>
            <Text style={s.label}>
              Open P&L now ·{" "}
              {instrument === "ALL" ? "all instruments" : instrument}
            </Text>
            <Text
              style={[
                s.amount,
                Number(data.current_open.unrealized) < 0 && s.loss,
              ]}
            >
              {money(data.current_open.unrealized)}
            </Text>
            <Text style={s.note}>
              {data.current_open.positions} open positions · Prices{" "}
              {data.current_open.mark_status}. This estimate is separate from
              the historical total above.
            </Text>
          </View>
          <Chip
            label={showTrades ? "Hide trade breakdown" : "Show trade breakdown"}
            active={false}
            onPress={() => setShowTrades(!showTrades)}
          />
          {showTrades &&
            data.trades.map((t: Data) => (
              <View key={t.signal_id} style={s.detail}>
                <Text style={s.text}>{t.symbol}</Text>
                <Text style={s.note}>
                  {t.status} · Realized within selection
                </Text>
                <Text
                  style={[s.amount, Number(t.period_realized) < 0 && s.loss]}
                >
                  {money(t.period_realized)}
                </Text>
              </View>
            ))}
          <Text style={s.note}>
            Weeks start Monday. All reporting dates use India time. Balance
            adjustments are excluded.
          </Text>
        </>
      )}
    </View>
  );
}
const s = StyleSheet.create({
  panel: {
    backgroundColor: "#101e30",
    borderWidth: 1,
    borderColor: "#213148",
    borderRadius: 20,
    padding: 20,
    gap: 14,
  },
  title: { fontSize: 22, color: "#eef5ff", fontWeight: "700" },
  note: { fontSize: 12, lineHeight: 19, color: "#94a9c2" },
  label: { fontSize: 13, fontWeight: "600", color: "#c9d8e9" },
  text: { fontSize: 14, color: "#e2edf8", lineHeight: 21 },
  wrap: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  chip: {
    backgroundColor: "#21354c",
    paddingVertical: 11,
    paddingHorizontal: 13,
    borderRadius: 10,
  },
  active: { backgroundColor: "#55e4c5" },
  chipText: { fontSize: 12, fontWeight: "600", color: "#cfddee" },
  input: {
    borderWidth: 1,
    borderColor: "#3a526d",
    borderRadius: 10,
    padding: 12,
    color: "#eef5ff",
  },
  total: { fontSize: 34, fontWeight: "700", color: "#64e3bd" },
  amount: { fontSize: 17, fontWeight: "600", color: "#64e3bd" },
  loss: { color: "#ff889a" },
  error: { color: "#ff889a", lineHeight: 20 },
  warning: { fontSize: 12, color: "#f4c984", lineHeight: 19 },
  barColumn: { width: 52, padding: 6, borderRadius: 7, alignItems: "center" },
  bar: {
    width: 24,
    backgroundColor: "#55d8b6",
    borderRadius: 3,
    alignSelf: "center",
  },
  zero: { width: 40, height: 1, backgroundColor: "#7b91a8" },
  axis: { fontSize: 10, color: "#a3b6cd", marginTop: 7 },
  detail: { padding: 13, gap: 7, backgroundColor: "#0b1727", borderRadius: 12 },
  row: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    borderBottomWidth: 1,
    borderColor: "#21354b",
    paddingVertical: 12,
    gap: 8,
  },
});
