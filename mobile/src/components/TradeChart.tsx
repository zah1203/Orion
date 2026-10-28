import React, { useState } from "react";
import { View, Text, Pressable, StyleSheet } from "react-native";
import Svg, { Rect, Line, Circle, Text as SvgText } from "react-native-svg";

type Data = Record<string, any>;
const money = (n: unknown) => n == null ? "Unavailable" : "₹" + Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const time = (at: string) => new Date(at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
export default function TradeChart({ data }: { data: Data }) {
  const [series, setSeries] = useState("ltp");
  const [levels, setLevels] = useState(false);
  const [width, setWidth] = useState(360);
  const [selectedTime, setSelectedTime] = useState<string | null>(null);
  const [interval, setInterval] = useState("10");
  const chart = data.price_chart;
  const candles: Data[] = chart?.candles?.[interval] || [];
  const intervalMs = Number(interval) * 60000;
  const points: Data[] = chart?.segments.flat() || [];
  const selected = candles.find((p) => p.at === selectedTime);
  const guides = [
    { label: "Entry", price: data.entry_price, color: "#7bb9ff" },
    { label: "Current stop", price: data.current_stop, color: "#ff98a6" },
    ...data.targets.map((price: string, i: number) => ({ label: "T" + (i + 1) + (data.targets_hit?.includes(i + 1) ? " hit" : ""), price, color: "#80e1bc" })),
  ].filter((v) => v.price != null && Number.isFinite(Number(v.price)));
  const allMarkers: Data[] = chart?.markers || [];
  const begin = candles.length ? Date.parse(candles[0].at) : 0;
  const end = candles.length ? Date.parse(candles[candles.length - 1].at) + intervalMs : begin + intervalMs;
  const markers = allMarkers.filter((m) => Date.parse(m.at) >= begin && Date.parse(m.at) <= end);
  const prices = [...candles.flatMap((p) => [Number(p[series].low), Number(p[series].high)]), ...(levels ? [...markers.map((m) => Number(m.price)), ...guides.map((g) => Number(g.price))] : [])];
  const low = Math.min(...prices, prices[0] || 0), high = Math.max(...prices, prices[0] || 1);
  const padding = Math.max((high - low) * 0.12, 1);
  const bottom = low - padding, top = high + padding;
  const x = (at: string) => 56 + (Date.parse(at) - begin) / (end - begin) * 284;
  const y = (price: unknown) => 20 + (top - Number(price)) / (top - bottom) * 210;
  const latest = points[points.length - 1];
  const candleX = (at: string) => x(new Date(Date.parse(at) + intervalMs / 2).toISOString());
  const bodyWidth = Math.max(1, Math.min(12, intervalMs / (end - begin) * 284 * 0.65));
  const inScale = (price: unknown) => Number(price) >= bottom && Number(price) <= top;
  return <View style={s.card}>
    <Text style={s.heading}>Contract price chart</Text>
    <Text style={s.note}>{data.symbol} · {series === "ltp" ? "Last traded price" : "Broker bid / exit valuation"}</Text>
    <View style={s.row}>
      {["ltp", "bid"].map((v) => <Pressable accessibilityRole="button" accessibilityState={{ selected: series === v }} key={v} onPress={() => setSeries(v)} style={[s.chip, series === v && s.active]}><Text style={s.text}>{v.toUpperCase()}</Text></Pressable>)}
    </View>
    <View style={s.row}>{["5", "10", "15", "30", "60"].map((v) => <Pressable key={v} accessibilityRole="button" accessibilityState={{ selected: interval === v }} onPress={() => { setInterval(v); setSelectedTime(null); }} style={[s.chip, interval === v && s.active]}><Text style={s.text}>{v === "60" ? "1 hour" : v + " min"}</Text></Pressable>)}</View>
    <Text style={s.note}>{data.status === "CLOSED" ? "Exit day" : "Today"} (IST) · All available candles for this contract. Entries and exits are listed below.</Text>
    <Text style={s.note}>Green = close above open; red = close below open. Candles use recorded quotes, not complete exchange history. Faded candles have incomplete coverage.</Text>
    {chart?.window !== "DAY" ? <Text style={s.note}>Loading selected range…</Text> : candles.length && latest ? <>
      <Text style={s.heading}>{money(latest[series])} <Text style={s.note}>last plotted observation</Text></Text>
      <Text style={s.note}>{time(latest.at)} IST · Quote freshness is shown in the position summary above.</Text>
      <View onLayout={(e) => setWidth(e.nativeEvent.layout.width)}>
        <Svg width="100%" height={270} viewBox="0 0 360 270" preserveAspectRatio="none" accessibilityLabel={`${interval}-minute candlestick chart for ${data.symbol}`}>
          {[0, 1, 2, 3].map((i) => { const price = bottom + (top - bottom) * i / 3; return <React.Fragment key={i}>
            <Line x1={56} x2={340} y1={y(price)} y2={y(price)} stroke="#294052" />
            <SvgText x={50} y={y(price) + 4} textAnchor="end" fill="#a5bbce" fontSize={10}>{price.toFixed(1)}</SvgText>
          </React.Fragment>; })}
          {levels && guides.filter((g) => inScale(g.price)).map((g) => <Line key={g.label} x1={56} x2={340} y1={y(g.price)} y2={y(g.price)} stroke={g.color} strokeDasharray="5 5" opacity={0.7} />)}
          {candles.map((c) => {
            const v = c[series], color = Number(v.close) >= Number(v.open) ? "#55d6aa" : "#ff7887";
            return <React.Fragment key={c.at}>
              <Line x1={candleX(c.at)} x2={candleX(c.at)} y1={y(v.high)} y2={y(v.low)} stroke={color} opacity={c.partial ? 0.5 : 1} />
              <Rect x={candleX(c.at) - bodyWidth / 2} y={Math.min(y(v.open), y(v.close))} width={bodyWidth} height={Math.max(1, Math.abs(y(v.open) - y(v.close)))} fill={color} opacity={c.partial ? 0.5 : 1} />
            </React.Fragment>;
          })}
          {markers.filter((m) => inScale(m.price)).map((m, i) => <React.Fragment key={i}>
            <Circle cx={x(m.at)} cy={y(m.price)} r={6} fill={m.label === "Entry" ? "#7bb9ff" : "#ffc477"} stroke="#07111f" />
            <SvgText x={x(m.at)} y={y(m.price) - 10} textAnchor="middle" fill="#e1eefb" fontSize={10}>{m.label === "Entry" ? "B" : "S"}</SvgText>
          </React.Fragment>)}
          {selected && <><Line x1={candleX(selected.at)} x2={candleX(selected.at)} y1={20} y2={230} stroke="#e1eefb" strokeDasharray="3 3" /><Circle cx={candleX(selected.at)} cy={y(selected[series].close)} r={4} fill="#fff" /></>}
          <SvgText x={56} y={254} fill="#a5bbce" fontSize={9}>{time(new Date(begin).toISOString())}</SvgText>
          <SvgText x={340} y={254} textAnchor="end" fill="#a5bbce" fontSize={9}>{time(new Date(end).toISOString())}</SvgText>
        </Svg>
        <Pressable style={StyleSheet.absoluteFill} accessibilityRole="button" accessibilityLabel="Tap chart to inspect the nearest recorded price" onPress={(e) => {
          const at = begin + ((e.nativeEvent.locationX / width * 360 - 56) / 284) * (end - begin);
          const nearest = candles.reduce((best, p) => Math.abs(Date.parse(p.at) - at) < Math.abs(Date.parse(best.at) - at) ? p : best);
          setSelectedTime(nearest.at);
        }} />
      </View>
      <Text style={s.note}>{selected ? time(selected.at) + ` IST · ${interval} min · O ${money(selected[series].open)} · H ${money(selected[series].high)} · L ${money(selected[series].low)} · C ${money(selected[series].close)} · ${selected.count} observations${selected.partial ? " · Incomplete coverage" : ""}` : "Tap a candle to inspect open, high, low and close. B = entry; S = exit or partial exit. Times are IST."}</Text>
      <Pressable accessibilityRole="button" onPress={() => setLevels(!levels)}><Text style={s.link}>{levels ? "Hide price levels to zoom into price movement" : "Fit entry, stop and targets"}</Text></Pressable>
    </> : <Text style={s.note}>No retained broker quotes in this window. Execution prices below are not a price-history chart.</Text>}
    <View style={s.row}>{guides.map((g) => <Text key={g.label} style={[s.note, { color: g.color }]}>{g.label} {money(g.price)}</Text>)}</View>
    <Text style={s.note}>Stop line shows the current stop, not historical stop changes.</Text>
    {allMarkers.map((m, i) => <Text key={i} style={s.note}>{m.label} · {money(m.price)} · {time(m.at)} IST</Text>)}
    {chart?.truncated && <Text style={s.warning}>Limited to the most recent retained observations in this window.</Text>}
    <Text style={s.note}>Empty intervals are left blank. Only retained quote coverage is shown; older entry/exit events remain listed above. LTP candles show traded prices; bid candles show observed exit valuations.</Text>
  </View>;
}
const s = StyleSheet.create({
  card: { backgroundColor: "#102238", borderRadius: 16, padding: 16, gap: 12, borderColor: "#284259", borderWidth: 1 },
  row: { flexDirection: "row", flexWrap: "wrap", gap: 10 }, chip: { padding: 10, borderRadius: 10, backgroundColor: "#152f42" }, active: { backgroundColor: "#28604f" },
  text: { color: "#e1eefb" }, heading: { color: "#e1eefb", fontSize: 18, fontWeight: "600" }, note: { color: "#a5bbce", fontSize: 12, lineHeight: 19 }, link: { color: "#80e1bc" }, warning: { color: "#ffc477" },
});
