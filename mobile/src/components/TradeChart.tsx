import React, { useState } from "react";
import { View, Text, Pressable, StyleSheet } from "react-native";
import Svg, { Path, Line, Circle, Text as SvgText } from "react-native-svg";

type Data = Record<string, any>;
const money = (n: unknown) => n == null ? "Unavailable" : "₹" + Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const time = (at: string) => new Date(at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
export default function TradeChart({ data, window, onWindow }: { data: Data; window: string; onWindow: (v: string) => void }) {
  const [series, setSeries] = useState("ltp");
  const [levels, setLevels] = useState(true);
  const [width, setWidth] = useState(360);
  const [selectedTime, setSelectedTime] = useState<string | null>(null);
  const chart = data.price_chart;
  const points: Data[] = chart?.segments.flat() || [];
  const selected = points.find((p) => p.at === selectedTime);
  const guides = [
    { label: "Entry", price: data.entry_price, color: "#7bb9ff" },
    { label: "Current stop", price: data.current_stop, color: "#ff98a6" },
    ...data.targets.map((price: string, i: number) => ({ label: "T" + (i + 1) + (data.targets_hit?.includes(i + 1) ? " hit" : ""), price, color: "#80e1bc" })),
  ].filter((v) => v.price != null && Number.isFinite(Number(v.price)));
  const markers: Data[] = chart?.markers || [];
  const prices = [...points.map((p) => Number(p[series])), ...markers.map((m) => Number(m.price)), ...(levels ? guides.map((g) => Number(g.price)) : [])];
  const low = Math.min(...prices, prices[0] || 0), high = Math.max(...prices, prices[0] || 1);
  const padding = Math.max((high - low) * 0.12, 1);
  const bottom = low - padding, top = high + padding;
  const timestamps = [...points.map((p) => Date.parse(p.at)), ...markers.map((m) => Date.parse(m.at))];
  const begin = Math.min(...timestamps, timestamps[0] || 0);
  const end = Math.max(...timestamps, begin + 1000);
  const x = (at: string) => 56 + (Date.parse(at) - begin) / (end - begin) * 284;
  const y = (price: unknown) => 20 + (top - Number(price)) / (top - bottom) * 210;
  const latest = points[points.length - 1];
  return <View style={s.card}>
    <Text style={s.heading}>Contract price chart</Text>
    <Text style={s.note}>{data.symbol} · {series === "ltp" ? "Last traded price" : "Broker bid / exit valuation"}</Text>
    <View style={s.row}>
      {["1D", "1W", "1M"].map((v) => <Pressable accessibilityRole="button" accessibilityState={{ selected: window === v }} key={v} onPress={() => { setSelectedTime(null); onWindow(v); }} style={[s.chip, window === v && s.active]}><Text style={s.text}>{v}</Text></Pressable>)}
      {["ltp", "bid"].map((v) => <Pressable accessibilityRole="button" accessibilityState={{ selected: series === v }} key={v} onPress={() => setSeries(v)} style={[s.chip, series === v && s.active]}><Text style={s.text}>{v.toUpperCase()}</Text></Pressable>)}
    </View>
    {chart?.window !== window ? <Text style={s.note}>Loading selected range…</Text> : points.length ? <>
      <Text style={s.heading}>{money(latest[series])} <Text style={s.note}>last plotted observation</Text></Text>
      <Text style={s.note}>{time(latest.at)} IST · Quote freshness is shown in the position summary above.</Text>
      <View onLayout={(e) => setWidth(e.nativeEvent.layout.width)}>
        <Svg width="100%" height={270} viewBox="0 0 360 270" preserveAspectRatio="none" accessibilityLabel="Recorded contract price observations with entry and exit markers">
          {[0, 1, 2, 3].map((i) => { const price = bottom + (top - bottom) * i / 3; return <React.Fragment key={i}>
            <Line x1={56} x2={340} y1={y(price)} y2={y(price)} stroke="#294052" />
            <SvgText x={50} y={y(price) + 4} textAnchor="end" fill="#a5bbce" fontSize={10}>{price.toFixed(1)}</SvgText>
          </React.Fragment>; })}
          {levels && guides.map((g) => <Line key={g.label} x1={56} x2={340} y1={y(g.price)} y2={y(g.price)} stroke={g.color} strokeDasharray="5 5" opacity={0.7} />)}
          {chart.segments.map((segment: Data[], i: number) => <React.Fragment key={i}>
            {segment.length > 1 && <Path d={segment.map((p, j) => (j ? "L" : "M") + x(p.at) + " " + y(p[series])).join(" ")} stroke="#55d6aa" strokeWidth={2} fill="none" />}
            {segment.length === 1 && <Circle cx={x(segment[0].at)} cy={y(segment[0][series])} r={2} fill="#55d6aa" />}
          </React.Fragment>)}
          {markers.map((m, i) => <React.Fragment key={i}>
            <Circle cx={x(m.at)} cy={y(m.price)} r={6} fill={m.label === "Entry" ? "#7bb9ff" : "#ffc477"} stroke="#07111f" />
            <SvgText x={x(m.at)} y={y(m.price) - 10} textAnchor="middle" fill="#e1eefb" fontSize={10}>{m.label === "Entry" ? "B" : "S"}</SvgText>
          </React.Fragment>)}
          {selected && <><Line x1={x(selected.at)} x2={x(selected.at)} y1={20} y2={230} stroke="#e1eefb" strokeDasharray="3 3" /><Circle cx={x(selected.at)} cy={y(selected[series])} r={4} fill="#fff" /></>}
          <SvgText x={56} y={254} fill="#a5bbce" fontSize={9}>{time(new Date(begin).toISOString())}</SvgText>
          <SvgText x={340} y={254} textAnchor="end" fill="#a5bbce" fontSize={9}>{time(new Date(end).toISOString())}</SvgText>
        </Svg>
        <Pressable style={StyleSheet.absoluteFill} accessibilityRole="button" accessibilityLabel="Tap chart to inspect the nearest recorded price" onPress={(e) => {
          const at = begin + ((e.nativeEvent.locationX / width * 360 - 56) / 284) * (end - begin);
          const nearest = points.reduce((best, p) => Math.abs(Date.parse(p.at) - at) < Math.abs(Date.parse(best.at) - at) ? p : best);
          setSelectedTime(nearest.at);
        }} />
      </View>
      <Text style={s.note}>{selected ? time(selected.at) + " IST · LTP " + money(selected.ltp) + " · Bid " + money(selected.bid) : "Tap the chart to inspect a recorded price. B = entry; S = exit or partial exit. Times are IST."}</Text>
      <Pressable accessibilityRole="button" onPress={() => setLevels(!levels)}><Text style={s.link}>{levels ? "Hide price levels to zoom into price movement" : "Show entry, stop and targets"}</Text></Pressable>
    </> : <Text style={s.note}>No retained broker quotes in this window. Execution prices below are not a price-history chart.</Text>}
    <View style={s.row}>{guides.map((g) => <Text key={g.label} style={[s.note, { color: g.color }]}>{g.label} {money(g.price)}</Text>)}</View>
    <Text style={s.note}>Stop line shows the current stop, not historical stop changes.</Text>
    {markers.map((m, i) => <Text key={i} style={s.note}>{m.label} · {money(m.price)} · {time(m.at)} IST</Text>)}
    {chart?.truncated && <Text style={s.warning}>Limited to the most recent retained observations in this window.</Text>}
    <Text style={s.note}>{chart?.note}</Text>
  </View>;
}
const s = StyleSheet.create({
  card: { backgroundColor: "#102238", borderRadius: 16, padding: 16, gap: 12, borderColor: "#284259", borderWidth: 1 },
  row: { flexDirection: "row", flexWrap: "wrap", gap: 10 }, chip: { padding: 10, borderRadius: 10, backgroundColor: "#152f42" }, active: { backgroundColor: "#28604f" },
  text: { color: "#e1eefb" }, heading: { color: "#e1eefb", fontSize: 18, fontWeight: "600" }, note: { color: "#a5bbce", fontSize: 12, lineHeight: 19 }, link: { color: "#80e1bc" }, warning: { color: "#ffc477" },
});
