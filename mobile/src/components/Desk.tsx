import React, { useState } from "react";
import { View, Text, Pressable, StyleSheet } from "react-native";
import { LinearGradient } from "expo-linear-gradient";
import { Ionicons } from "@expo/vector-icons";
type Data = Record<string, any>;
const money = (n: unknown) =>
  n == null
    ? "Unavailable"
    : "₹" + Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 });
export function Panel({ children }: { children: React.ReactNode }) {
  return (
    <LinearGradient colors={["#132840", "#091a2d"]} style={s.panel}>
      {children}
    </LinearGradient>
  );
}
function Icon({
  name,
  color = "#4af0ce",
}: {
  name: React.ComponentProps<typeof Ionicons>["name"];
  color?: string;
}) {
  return (
    <View style={[s.icon, { borderColor: color + "40" }]}>
      <Ionicons name={name} size={25} color={color} />
    </View>
  );
}
export function Action({
  label,
  onPress,
  primary = false,
  disabled = false,
}: {
  label: string;
  onPress: () => void;
  primary?: boolean;
  disabled?: boolean;
}) {
  return (
    <Pressable
      accessibilityRole="button"
      disabled={disabled}
      onPress={onPress}
      style={[s.button, primary && s.primary, disabled && { opacity: 0.4 }]}
    >
      <Text style={[s.buttonText, primary && { color: "#052b25" }]}>
        {label}
      </Text>
    </Pressable>
  );
}
function Heading({ title, onPress }: { title: string; onPress?: () => void }) {
  return (
    <View style={s.row}>
      <Text style={s.heading}>{title}</Text>
      {onPress && (
        <Pressable accessibilityRole="button" onPress={onPress}>
          <Text style={s.link}>View all →</Text>
        </Pressable>
      )}
    </View>
  );
}
function Curve({ daily }: { daily: Data[] }) {
  const [width, setWidth] = useState(280);
  const points = daily.reduce<number[]>(
    (values, d) => [...values, values[values.length - 1] + Number(d.realized)],
    [0],
  );
  const sum = points[points.length - 1];
  const min = Math.min(...points),
    max = Math.max(...points);
  const span = max - min || 1;
  const coords = points.map((v, i) => ({
    x: (i * (width - 8)) / (points.length - 1 || 1),
    y: 115 - ((v - min) / span) * 100,
  }));
  return (
    <View>
      <View
        onLayout={(e) => setWidth(e.nativeEvent.layout.width)}
        accessibilityLabel={`Cumulative realized profit and loss across ${daily.length} recorded days`}
        style={{ height: 130, overflow: "hidden" }}
      >
        {[25, 70, 115].map((y) => (
          <View
            key={y}
            style={{
              position: "absolute",
              top: y,
              left: 0,
              right: 0,
              height: 1,
              backgroundColor: "#23384d",
            }}
          />
        ))}
        {coords.slice(1).map((p, i) => {
          const a = coords[i],
            dx = p.x - a.x,
            dy = p.y - a.y;
          return (
            <View
              key={i}
              style={{
                position: "absolute",
                left: (a.x + p.x) / 2 - Math.hypot(dx, dy) / 2,
                top: (a.y + p.y) / 2,
                width: Math.hypot(dx, dy),
                height: 2,
                backgroundColor: sum < 0 ? "#ff889a" : "#4af0ce",
                transform: [{ rotate: `${Math.atan2(dy, dx)}rad` }],
              }}
            />
          );
        })}
      </View>
      <View style={s.row}>
        <Text style={s.note}>
          {daily[0]?.date || "No realized activity yet"}
        </Text>
        <Text style={s.note}>{daily[daily.length - 1]?.date}</Text>
      </View>
      <Text style={s.note}>
        Cumulative realized P&L · Recorded ledger days · All time
      </Text>
    </View>
  );
}
export function Portfolio({ me }: { me: Data }) {
  return (
    <Panel>
      <View style={s.row}>
        <Text style={s.heading}>Your paper portfolio</Text>
        <Text style={s.badge}>ALL TIME</Text>
      </View>
      <Text style={s.balance}>{money(me.pnl.totals.equity)}</Text>
      <Text style={s.note}>
        Paper equity · Includes {me.pnl.totals.mark_status} open marks
      </Text>
      <View style={s.row}>
        <View>
          <Text style={s.note}>Realized P&L</Text>
          <Text
            style={[s.profit, Number(me.pnl.totals.realized) < 0 && s.loss]}
          >
            {money(me.pnl.totals.realized)}
          </Text>
        </View>
        <View>
          <Text style={s.note}>Available cash</Text>
          <Text style={s.text}>{money(me.pnl.totals.cash)}</Text>
        </View>
      </View>
      <Curve daily={me.pnl.daily || []} />
    </Panel>
  );
}
export function Connections({
  me,
  onPress,
}: {
  me: Data;
  onPress: () => void;
}) {
  return (
    <View style={s.grid}>
      {[
        ["Telegram", "paper-plane", "telegram"],
        ["Kotak Neo", "wallet", "broker"],
      ].map(([label, icon, key]) => {
        const status = me.worker_online
          ? me.worker_health?.[key] || "Waiting"
          : "Worker offline";
        return (
          <Pressable
            key={key}
            accessibilityRole="button"
            accessibilityLabel={`Open ${label} connection`}
            onPress={onPress}
            style={s.tile}
          >
            <Icon
              name={icon as any}
              color={key === "telegram" ? "#48b8ff" : "#ff7885"}
            />
            <Text style={s.strong}>{label}</Text>
            <Text
              style={[s.note, status === "connected" && { color: "#4af0ce" }]}
            >
              ● {status}
            </Text>
          </Pressable>
        );
      })}
    </View>
  );
}
export function OpenPreview({
  me,
  onPress,
}: {
  me: Data;
  onPress: () => void;
}) {
  const positions = me.pnl.positions.filter((p: Data) => p.remaining > 0);
  return (
    <>
      <Heading
        title={`Open positions (${positions.length})`}
        onPress={onPress}
      />
      {positions.length === 0 ? (
        <Panel>
          <Text style={s.note}>
            No open paper positions. Filled trades will appear here.
          </Text>
        </Panel>
      ) : (
        positions.slice(0, 2).map((p: Data) => (
          <Pressable
            key={p.signal_id}
            accessibilityRole="button"
            onPress={onPress}
          >
            <Panel>
              <View style={s.row}>
                <Icon name="layers-outline" />
                <Text style={[s.strong, { flex: 1 }]}>{p.symbol}</Text>
                <Text
                  style={[
                    s.badge,
                    p.mark_status !== "fresh" && { color: "#f3c36b" },
                  ]}
                >
                  {p.mark_status}
                </Text>
              </View>
              <Text style={s.note}>
                {p.remaining} lots · Entry {money(p.entry)} · Stop{" "}
                {money(p.stop)}
              </Text>
              <View style={s.divider} />
              <Text style={s.note}>Last-known open P&L</Text>
              <Text style={[s.profit, Number(p.unrealized) < 0 && s.loss]}>
                {money(p.unrealized)}
              </Text>
            </Panel>
          </Pressable>
        ))
      )}
    </>
  );
}
export function ChannelResults({
  me,
  onResults,
  onConnect,
}: {
  me: Data;
  onResults: (id: string) => void;
  onConnect: () => void;
}) {
  return (
    <>
      <Text style={s.note}>Measure your channels with virtual capital</Text>
      {(me.pnl.channels || []).map((c: Data) => (
        <Panel key={c.channel_id}>
          <View style={s.row}>
            <Icon
              name={
                me.settings.channels[c.channel_id]?.profile === "index"
                  ? "trending-up"
                  : "albums-outline"
              }
              color={
                me.settings.channels[c.channel_id]?.profile === "index"
                  ? "#4af0ce"
                  : "#f3c36b"
              }
            />
            <View style={{ flex: 1 }}>
              <Text style={s.heading}>{c.name}</Text>
              <Text style={s.note}>
                {c.trades} filled trades · {c.closed} closed
              </Text>
            </View>
          </View>
          <View style={s.divider} />
          <View style={s.row}>
            <View>
              <Text style={s.note}>Realized paper P&L · All time</Text>
              <Text style={[s.profit, Number(c.realized) < 0 && s.loss]}>
                {money(c.realized)}
              </Text>
            </View>
            <Action
              label="View results"
              onPress={() => onResults(c.channel_id)}
            />
          </View>
        </Panel>
      ))}
      <Pressable accessibilityRole="button" onPress={onConnect}>
        <Panel>
          <View style={s.row}>
            <Icon name="paper-plane" color="#48b8ff" />
            <View style={{ flex: 1 }}>
              <Text style={s.heading}>Telegram connection</Text>
              <Text style={s.note}>Manage your account and connection</Text>
            </View>
            <Ionicons name="chevron-forward" color="#88b3d9" size={24} />
          </View>
        </Panel>
      </Pressable>
    </>
  );
}
export function RecentSignals({
  history,
  onPress,
}: {
  history: Data[];
  onPress: () => void;
}) {
  return (
    <>
      <Heading title="Recent signal activity" onPress={onPress} />
      {history.length === 0 ? (
        <Panel>
          <Text style={s.note}>Waiting for channel activity.</Text>
        </Panel>
      ) : (
        history.slice(0, 3).map((e: Data, i: number) => (
          <Panel key={i}>
            <View style={s.row}>
              <Icon name="pulse-outline" color="#74b9ff" />
              <View style={{ flex: 1 }}>
                <Text style={s.strong}>{e.event.replaceAll("_", " ")}</Text>
                <Text style={s.note}>{e.signal_id || "Account event"}</Text>
              </View>
            </View>
            {(e.reason || e.detail) && (
              <Text style={s.note}>{e.reason || e.detail}</Text>
            )}
            <Text style={s.note}>{new Date(e.at).toLocaleString()}</Text>
          </Panel>
        ))
      )}
    </>
  );
}
export function OwnerSummary({
  users,
  onAccess,
  onUser,
  busy,
}: {
  users: Data[];
  onAccess: (id: string, access: string) => void;
  onUser: (id: string) => void;
  busy: boolean;
}) {
  const pending = users.filter((u) => u.access === "pending");
  const offline = users.filter((u) => u.enabled && !u.worker_online);
  return (
    <>
      <Text style={s.note}>
        Manage access, paper performance and worker health
      </Text>
      <Panel>
        <View style={s.row}>
          <Icon name="people-outline" color="#a69aff" />
          <View style={{ flex: 1 }}>
            <Text style={s.heading}>Access requests</Text>
            <Text
              style={[
                s.note,
                { color: pending.length ? "#ff889a" : "#8fa9c2" },
              ]}
            >
              {pending.length} awaiting approval
            </Text>
          </View>
          <Text style={s.admin}>ADMIN</Text>
        </View>
        {pending.map((u) => (
          <View key={u.id} style={{ gap: 12 }}>
            <View style={s.divider} />
            <View style={s.row}>
              <View style={s.avatar}>
                <Text style={s.text}>
                  {u.username.slice(0, 2).toUpperCase()}
                </Text>
              </View>
              <Text style={[s.strong, { flex: 1 }]}>{u.username}</Text>
            </View>
            <View style={s.row}>
              <Action
                label={`Approve ${u.username}`}
                primary
                disabled={busy}
                onPress={() => onAccess(u.id, "approved")}
              />
              <Action
                label={`Decline ${u.username}`}
                disabled={busy}
                onPress={() => onAccess(u.id, "suspended")}
              />
            </View>
          </View>
        ))}
      </Panel>
      <Heading title="System overview" />
      <View style={s.grid}>
        {[
          [
            "Approved users",
            users.filter((u) => u.access === "approved").length,
          ],
          ["Active workers", users.filter((u) => u.worker_online).length],
        ].map(([label, value]) => (
          <View key={label} style={s.tile}>
            <Text style={s.note}>{label}</Text>
            <Text style={s.balance}>{value}</Text>
            <Text style={s.note}>Current status</Text>
          </View>
        ))}
      </View>
      {offline.map((u) => (
        <Pressable
          key={u.id}
          onPress={() => onUser(u.id)}
          accessibilityRole="button"
        >
          <View style={s.warning}>
            <Ionicons name="warning-outline" color="#ff889a" size={30} />
            <View style={{ flex: 1 }}>
              <Text style={s.strong}>{u.username} needs attention</Text>
              <Text style={s.note}>Entries enabled · Worker offline →</Text>
            </View>
          </View>
        </Pressable>
      ))}
    </>
  );
}
const s = StyleSheet.create({
  panel: {
    padding: 20,
    borderRadius: 22,
    borderWidth: 1,
    borderColor: "#29435f",
    gap: 14,
  },
  row: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 12,
    flexWrap: "wrap",
  },
  grid: { flexDirection: "row", gap: 12, flexWrap: "wrap" },
  tile: {
    flex: 1,
    minWidth: 140,
    padding: 18,
    borderRadius: 20,
    borderWidth: 1,
    borderColor: "#29435f",
    backgroundColor: "#10243b",
    gap: 12,
  },
  heading: { fontSize: 19, fontWeight: "600", color: "#eef5ff", flexShrink: 1 },
  text: { fontSize: 15, color: "#dce9f8" },
  strong: { fontSize: 15, fontWeight: "600", color: "#e5f0ff" },
  note: { fontSize: 12, lineHeight: 19, color: "#9ab1c9" },
  balance: { fontSize: 36, fontWeight: "700", color: "#f2f7ff" },
  profit: { fontSize: 25, fontWeight: "700", color: "#4af0ce" },
  loss: { color: "#ff889a" },
  icon: {
    width: 48,
    height: 48,
    borderRadius: 15,
    backgroundColor: "#18344d",
    borderWidth: 1,
    alignItems: "center",
    justifyContent: "center",
  },
  button: {
    borderWidth: 1,
    borderColor: "#446b91",
    borderRadius: 12,
    padding: 13,
    backgroundColor: "#122d47",
  },
  buttonText: { color: "#bedfff", fontSize: 13, fontWeight: "600" },
  primary: { backgroundColor: "#4af0ce", borderColor: "#4af0ce" },
  badge: {
    color: "#8db4d7",
    fontSize: 10,
    fontWeight: "700",
    letterSpacing: 1,
  },
  admin: {
    color: "#c7adff",
    backgroundColor: "#2e244c",
    padding: 9,
    borderRadius: 10,
    fontSize: 11,
    fontWeight: "700",
  },
  divider: { height: 1, backgroundColor: "#29405a" },
  link: { color: "#78bdff", fontSize: 13 },
  avatar: {
    width: 40,
    height: 40,
    borderRadius: 20,
    backgroundColor: "#294761",
    alignItems: "center",
    justifyContent: "center",
  },
  warning: {
    padding: 18,
    borderRadius: 18,
    backgroundColor: "#382a36",
    borderWidth: 1,
    borderColor: "#654050",
    flexDirection: "row",
    alignItems: "center",
    gap: 14,
  },
});
