import React, { useEffect, useState, useRef } from "react";
import {
  View,
  Text,
  TextInput,
  Pressable,
  ScrollView,
  StyleSheet,
  RefreshControl,
  AppState,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { LinearGradient } from "expo-linear-gradient";
import { Ionicons } from "@expo/vector-icons";
import {
  Portfolio,
  Connections,
  OpenPreview,
  ChannelResults,
  RecentSignals,
  OwnerSummary,
} from "../components/Desk";
import Performance from "../components/Performance";
import { api, login, logout, register, restore } from "../lib/api";
import {
  phoneAlertsSupported,
  enablePhoneAlerts,
  disablePhoneAlerts,
  watchNotificationTap,
} from "../lib/notifications";
type Obj = Record<string, any>;
const money = (n: unknown) =>
  n === null || n === undefined
    ? "Unavailable"
    : "₹" + Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const when = (s: string) => (s ? new Date(s).toLocaleString() : "Not yet");
function Button({
  label,
  onPress,
  muted = false,
  disabled = false,
}: {
  label: string;
  onPress: () => void;
  muted?: boolean;
  disabled?: boolean;
}) {
  return (
    <Pressable
      accessibilityRole="button"
      disabled={disabled}
      onPress={onPress}
      style={[s.button, muted && s.muted, disabled && { opacity: 0.4 }]}
    >
      <Text style={[s.buttonText, muted && { color: "#d6e5f3" }]}>{label}</Text>
    </Pressable>
  );
}
function Field({
  label,
  value,
  onChange,
  secret = false,
}: {
  label: string;
  value: string;
  onChange: (s: string) => void;
  secret?: boolean;
}) {
  return (
    <View style={{ gap: 7 }}>
      <Text style={s.label}>{label}</Text>
      <TextInput
        accessibilityLabel={label}
        autoCapitalize="none"
        autoCorrect={false}
        secureTextEntry={secret}
        value={value}
        onChangeText={onChange}
        style={s.input}
        placeholderTextColor="#70839b"
      />
    </View>
  );
}
function Card({
  title,
  children,
}: {
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <View style={s.card}>
      {title && <Text style={s.cardTitle}>{title}</Text>}
      {children}
    </View>
  );
}
function Metrics({ p }: { p: Obj }) {
  return (
    <>
      <View style={s.metrics}>
        {[
          ["Realized P&L", p.realized],
          ["Open P&L", p.unrealized],
        ].map(([k, v]) => (
          <View key={k} style={s.metric}>
            <Text style={s.label}>{k}</Text>
            <Text style={[s.value, Number(v) < 0 && { color: "#ff889a" }]}>
              {money(v)}
            </Text>
          </View>
        ))}
      </View>
      <Text style={s.note}>
        {p.trades} filled trades · {p.open_lots} open lots · Prices:{" "}
        {p.mark_status}
      </Text>
    </>
  );
}
function Positions({ data }: { data: Obj }) {
  return (
    <>
      {data.pnl.positions.length === 0 && (
        <Card>
          <Text style={s.text}>
            No filled paper trades yet. Signal decisions appear in Activity.
          </Text>
        </Card>
      )}
      {[...data.pnl.positions].reverse().map((p: Obj) => (
        <Card key={p.signal_id} title={p.symbol}>
          <View style={s.row}>
            <Text style={s.tag}>
              {p.status} · {p.remaining} lots left
            </Text>
            <Text style={s.value}>{money(p.total)}</Text>
          </View>
          <Text style={s.note}>
            Entry {p.entry} · Stop {p.stop} · {p.mark_status} prices
          </Text>
          <Text style={s.note}>
            Entered {when(p.entry_time)}
            {p.exit_time ? " · Exited " + when(p.exit_time) : ""}
          </Text>
          <Text style={s.note}>
            Realized {money(p.realized)} · Open {money(p.unrealized)}
          </Text>
        </Card>
      ))}
    </>
  );
}
export default function App() {
  const [me, setMe] = useState<Obj | null>(null),
    [tab, setTab] = useState("Home"),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [notice, setNotice] = useState("");
  const [username, setUsername] = useState(""),
    [password, setPassword] = useState(""),
    [signup, setSignup] = useState(false);
  const [adminAudit, setAdminAudit] = useState<Obj[]>([]);
  const [tradeChannel, setTradeChannel] = useState<string | null>(null);
  const [users, setUsers] = useState<Obj[]>([]),
    [selected, setSelected] = useState<Obj | null>(null),
    [events, setEvents] = useState<Obj[]>([]),
    [cursor, setCursor] = useState<number | null>(null);
  const [form, setForm] = useState<Obj>({}),
    [available, setAvailable] = useState<Obj[]>([]),
    [channels, setChannels] = useState<Obj[]>([]),
    [connection, setConnection] = useState<Obj>({});
  const [attention, setAttention] = useState<Obj>({ incidents: [] });
  useEffect(
    () =>
      watchNotificationTap(() => {
        setTab("Account");
        const version = generation.current;
        api("/me")
          .then((d) => {
            if (version === generation.current) setForm({ ...d.settings });
          })
          .catch(() => {});
        api("/connections")
          .then((d) => {
            if (version === generation.current) setConnection(d);
          })
          .catch(() => {});
        setNotice(
          "Check connection status below. Sign in again if your app session has expired.",
        );
      }),
    [],
  );
  const generation = useRef(0);
  async function refresh() {
    const version = generation.current;
    const d = await api("/me");
    const alerts = await api("/attention");
    if (version === generation.current) {
      setMe(d);
      setAttention(alerts);
    }
    return d;
  }
  useEffect(() => {
    restore()
      .then(refresh)
      .catch(() => {});
  }, []);
  useEffect(() => {
    const timer = setInterval(() => {
      if (me && AppState.currentState === "active")
        refresh().catch((e) => setError(e.message));
    }, 15000);
    return () => clearInterval(timer);
  }, [me?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  async function run(fn: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }
  const val = (k: string) => String(form[k] ?? "");
  const field = (k: string, label: string, secret = false) => (
    <Field
      key={k}
      label={label}
      value={val(k)}
      secret={secret}
      onChange={(v) => setForm((f) => ({ ...f, [k]: v }))}
    />
  );
  async function navigate(next: string) {
    setSelected(null);
    setTab(next);
    setForm({});
    setEvents([]);
    setCursor(null);
    if (next === "Owner") {
      const d = await api("/admin/users");
      setUsers(d.users);
      setAdminAudit(d.audit || []);
    }
    if (next === "Activity") {
      await loadActivity();
    }
    if (next === "Channels") {
      setChannels(
        Object.entries(me?.settings.channels || {}).map(([id, c]) => ({
          id,
          ...(c as Obj),
          profile:
            (c as Obj).profile ||
            ((c as Obj).products.includes("NIFTY") ||
            (c as Obj).products.includes("BANKNIFTY")
              ? "index"
              : "commodity"),
        })),
      );
    }
    if (next === "Account") {
      setForm({ ...me?.settings });
      setConnection(await api("/connections"));
    }
  }
  async function loadActivity(uid?: string, before?: number | null) {
    const d = await api(
      (uid ? "/admin/users/" + uid : "") +
        "/activity" +
        (before ? "?before=" + before : ""),
    );
    setEvents((old) => (before ? [...old, ...d.events] : d.events));
    setCursor(d.next_cursor);
  }
  async function connect(path: string, body: Obj) {
    const d = await api("/connections/" + path, body);
    setConnection(d);
    setForm((f) => ({ ...f, code: "", password: "", totp: "" }));
    if (d.channels) setAvailable(d.channels);
    setNotice(d.note || d.message || "Connection step completed");
    await refresh();
  }

  return (
    <SafeAreaView style={s.safe}>
      <View style={s.shell}>
        <View style={s.header}>
          <View>
            <View
              style={{ flexDirection: "row", alignItems: "center", gap: 9 }}
            >
              <Ionicons name="planet-outline" color="#70e5f2" size={29} />
              <Text style={s.brand}>ORION</Text>
            </View>
            <Text style={s.note}>Your signals. Measured.</Text>
          </View>
          {me && (
            <Pressable
              onPress={() =>
                run(async () => {
                  generation.current += 1;
                  setMe(null);
                  setAttention({ incidents: [] });
                  setSelected(null);
                  setForm({});
                  setPassword("");
                  setEvents([]);
                  setUsers([]);
                  setTab("Home");
                  await logout();
                })
              }
            >
              <Ionicons name="log-out-outline" size={24} color="#9bafc7" />
            </Pressable>
          )}
        </View>
        <ScrollView
          contentContainerStyle={s.content}
          refreshControl={
            <RefreshControl
              refreshing={busy}
              tintColor="#55e4c5"
              onRefresh={() =>
                run(async () => {
                  await refresh();
                })
              }
            />
          }
        >
          {error !== "" && (
            <View style={s.alert}>
              <Text style={s.text}>{error}</Text>
            </View>
          )}
          {notice !== "" && (
            <Card>
              <Text style={s.text}>{notice}</Text>
            </Card>
          )}
          {me &&
            attention.incidents.map((incident: Obj) => (
              <View key={incident.user_id} style={s.alert}>
                <Text style={s.cardTitle}>{incident.title}</Text>
                <Text style={s.text}>{incident.message}</Text>
                <Text style={s.note}>
                  Since {new Date(incident.since * 1000).toLocaleString()}
                  {incident.user_id !== me.id
                    ? " · Another user account (see Owner)"
                    : ""}
                </Text>
                <Button
                  label={
                    incident.user_id === me.id
                      ? "Check Kotak connection"
                      : "Open owner dashboard"
                  }
                  onPress={() =>
                    run(() =>
                      navigate(
                        incident.user_id === me.id ? "Account" : "Owner",
                      ),
                    )
                  }
                />
              </View>
            ))}
          {!me ? (
            <>
              <LinearGradient colors={["#173750", "#0b192c"]} style={s.hero}>
                <Text style={s.eyebrow}>
                  TEST THE SIGNAL. BUILD CONFIDENCE.
                </Text>
                <Text style={s.title}>A clearer view of every trade.</Text>
                <Text style={s.text}>
                  Connect your Telegram channels and test them with a virtual
                  balance.
                </Text>
              </LinearGradient>
              <Card title={signup ? "Create your account" : "Welcome back"}>
                <Field
                  label="Username"
                  value={username}
                  onChange={setUsername}
                />
                <Field
                  label="Password"
                  value={password}
                  onChange={setPassword}
                  secret
                />
                <Button
                  disabled={busy}
                  label={signup ? "Request access" : "Sign in"}
                  onPress={() =>
                    run(async () => {
                      if (signup) {
                        await register(username, password);
                        setSignup(false);
                        setPassword("");
                        setNotice(
                          "Account created. Sign in to check owner approval.",
                        );
                      } else {
                        await login(username, password);
                        setPassword("");
                        await refresh();
                      }
                    })
                  }
                />
                <Button
                  muted
                  label={
                    signup
                      ? "Already registered? Sign in"
                      : "New here? Create account"
                  }
                  onPress={() => setSignup(!signup)}
                />
                <Text style={s.note}>
                  Registration requires owner approval. Use at least 12
                  characters for your password.
                </Text>
              </Card>
            </>
          ) : me.access !== "approved" ? (
            <Card
              title={
                me.access === "pending"
                  ? "Your account is awaiting approval"
                  : "Access suspended"
              }
            >
              <Text style={s.text}>
                Your owner manages access to Orion. Your account cannot start
                new trades until approved.
              </Text>
              <Button
                label="Check status"
                onPress={() =>
                  run(async () => {
                    await refresh();
                  })
                }
              />
            </Card>
          ) : (
            <>
              <View style={s.row}>
                <Text style={s.title}>
                  {selected
                    ? selected.username
                    : tab === "Home"
                      ? "Your overview"
                      : tab === "Owner"
                        ? "Owner dashboard"
                        : tab === "Channels"
                          ? "Your channels"
                          : tab}
                </Text>
                <View style={s.mode}>
                  <Text style={s.tag}>● Paper</Text>
                  <Text
                    accessibilityLabel="Live mode unavailable"
                    style={s.note}
                  >
                    Live locked
                  </Text>
                </View>
              </View>
              {selected ? (
                <>
                  <Button
                    muted
                    label="← All users"
                    onPress={() => run(() => navigate("Owner"))}
                  />
                  <Text style={s.note}>
                    Account totals · since account started
                  </Text>
                  <Metrics p={selected.pnl.totals} />
                  <Performance key={selected.id} userId={selected.id} />
                  <Text style={s.note}>
                    {selected.access} ·{" "}
                    {selected.worker_online
                      ? "Worker online"
                      : "Worker offline"}
                  </Text>
                  {selected.role !== "owner" && (
                    <View style={s.row}>
                      <Button
                        label="Approve"
                        onPress={() =>
                          run(async () => {
                            await api(
                              "/admin/users/" + selected.id + "/access",
                              { access: "approved" },
                              "PUT",
                            );
                            setSelected(
                              await api("/admin/users/" + selected.id),
                            );
                          })
                        }
                      />
                      <Button
                        muted
                        label="Suspend access"
                        onPress={() =>
                          run(async () => {
                            await api(
                              "/admin/users/" + selected.id + "/access",
                              { access: "suspended" },
                              "PUT",
                            );
                            setSelected(
                              await api("/admin/users/" + selected.id),
                            );
                          })
                        }
                      />
                    </View>
                  )}
                  <Button
                    muted
                    label="Pause new entries"
                    onPress={() =>
                      run(async () => {
                        await api("/admin/users/" + selected.id + "/pause", {});
                        setSelected(await api("/admin/users/" + selected.id));
                      })
                    }
                  />
                  <Positions data={selected} />
                  <Button
                    muted
                    label="Load signal activity"
                    onPress={() => run(() => loadActivity(selected.id))}
                  />
                </>
              ) : null}
              {!selected && tab === "Home" && (
                <>
                  <Portfolio me={me} />
                  <Card
                    title={
                      me.enabled && me.worker_online
                        ? "Paper bot online"
                        : "Paper bot · " + (me.enabled ? "Waiting" : "Paused")
                    }
                  >
                    <Text style={s.text}>
                      {me.enabled ? "Entries enabled" : "Entries paused"} ·{" "}
                      {me.worker_online ? "Worker online" : "Worker offline"}
                    </Text>
                    <Text style={s.note}>
                      Telegram: {me.worker_health?.telegram || "Waiting"} ·
                      Kotak: {me.worker_health?.broker || "Waiting"} ·
                      Catalogue: {me.worker_health?.catalogue || "Waiting"}
                    </Text>
                    <Button
                      disabled={busy}
                      label={
                        me.enabled ? "Pause entries" : "Enable paper entries"
                      }
                      onPress={() =>
                        run(async () => {
                          await api("/control", { enabled: !me.enabled });
                          await refresh();
                        })
                      }
                    />
                    <Text style={s.note}>
                      Pausing cancels pending entries. Open positions remain
                      monitored by the server.
                    </Text>
                  </Card>
                  <Connections
                    me={me}
                    onPress={() => run(() => navigate("Account"))}
                  />
                  <OpenPreview
                    me={me}
                    onPress={() => {
                      setTradeChannel(null);
                      run(() => navigate("Trades"));
                    }}
                  />
                  <Performance key={me.id} />
                  <RecentSignals
                    history={me.history || []}
                    onPress={() => run(() => navigate("Activity"))}
                  />
                </>
              )}
              {!selected && tab === "Trades" && (
                <>
                  <Button
                    muted
                    label="View signal activity"
                    onPress={() => run(() => navigate("Activity"))}
                  />
                  {tradeChannel && (
                    <Button
                      muted
                      label="Show all channels"
                      onPress={() => setTradeChannel(null)}
                    />
                  )}
                  <Positions
                    data={
                      tradeChannel
                        ? {
                            ...me,
                            pnl: {
                              ...me.pnl,
                              positions: me.pnl.positions.filter(
                                (p: Obj) => p.channel_id === tradeChannel,
                              ),
                            },
                          }
                        : me
                    }
                  />
                </>
              )}
              {!selected && tab === "Channels" && (
                <>
                  <ChannelResults
                    me={me}
                    onConnect={() => run(() => navigate("Account"))}
                    onResults={(id) => {
                      setTradeChannel(id);
                      run(() => navigate("Trades"));
                    }}
                  />
                  <RecentSignals
                    history={me.history || []}
                    onPress={() => run(() => navigate("Activity"))}
                  />
                  <Card title="Add & manage channels">
                    <Text style={s.note}>
                      Pause entries and finish open positions before changing
                      channels. Choose only channels your Telegram account can
                      access.
                    </Text>
                    <Button
                      muted
                      label="Load my Telegram channels"
                      onPress={() =>
                        run(() => connect("telegram/channels", {}))
                      }
                    />
                    {available.map((c) => (
                      <Button
                        key={c.id}
                        muted
                        label={c.title + " · Add"}
                        onPress={() => {
                          if (!channels.some((x) => x.id === c.id))
                            setChannels([
                              ...channels,
                              {
                                id: c.id,
                                name: c.title,
                                profile: "commodity",
                                products: ["CRUDEOIL"],
                              },
                            ]);
                        }}
                      />
                    ))}
                  </Card>
                  {channels.map((c, i) => (
                    <Card key={c.id} title={c.name}>
                      <Text style={s.note}>{c.id}</Text>
                      <View style={s.row}>
                        {["index", "commodity"].map((p) => (
                          <Button
                            key={p}
                            muted={c.profile !== p}
                            label={p}
                            onPress={() =>
                              setChannels(
                                channels.map((v, j) =>
                                  j === i
                                    ? {
                                        ...v,
                                        profile: p,
                                        products:
                                          p === "index"
                                            ? ["NIFTY"]
                                            : ["CRUDEOIL"],
                                      }
                                    : v,
                                ),
                              )
                            }
                          />
                        ))}
                      </View>
                      <View style={s.wrap}>
                        {(c.profile === "index"
                          ? ["NIFTY", "BANKNIFTY"]
                          : [
                              "CRUDEOIL",
                              "CRUDEOILM",
                              "GOLD",
                              "GOLDM",
                              "SILVER",
                              "SILVERM",
                            ]
                        ).map((p) => (
                          <Button
                            key={p}
                            muted={!c.products.includes(p)}
                            label={p}
                            onPress={() =>
                              setChannels(
                                channels.map((v, j) =>
                                  j === i
                                    ? {
                                        ...v,
                                        products: v.products.includes(p)
                                          ? v.products.filter(
                                              (x: string) => x !== p,
                                            )
                                          : [...v.products, p],
                                      }
                                    : v,
                                ),
                              )
                            }
                          />
                        ))}
                      </View>
                      <Button
                        muted
                        label="Remove channel"
                        onPress={() =>
                          setChannels(channels.filter((_, j) => i !== j))
                        }
                      />
                    </Card>
                  ))}
                  <Button
                    disabled={busy}
                    label="Save channels"
                    onPress={() =>
                      run(async () => {
                        await api(
                          "/channels",
                          {
                            channels: channels.map(
                              ({ id, name, profile, products }) => ({
                                id,
                                name,
                                profile,
                                products,
                              }),
                            ),
                          },
                          "PUT",
                        );
                        await refresh();
                        setNotice("Channels saved");
                      })
                    }
                  />
                </>
              )}
              {!selected && tab === "Account" && (
                <>
                  <Card title="Phone alerts">
                    <Text style={s.text}>
                      Get notified when Kotak needs authentication or your
                      worker loses monitoring. Reminders repeat every 15 minutes
                      while unresolved.
                    </Text>
                    <Text style={s.note}>
                      {attention.registered_devices || 0} registered devices.
                      Owner devices also receive alerts for other accounts.
                      Notifications contain no trading balances or credentials.
                    </Text>
                    {attention.devices_with_errors > 0 && (
                      <Text style={s.text}>
                        Notification delivery needs checking. Re-enable alerts
                        and check phone permissions.
                      </Text>
                    )}
                    {phoneAlertsSupported ? (
                      <>
                        <Button
                          label="Enable alerts on this phone"
                          onPress={() =>
                            run(async () => {
                              await enablePhoneAlerts();
                              await refresh();
                              setNotice(
                                "Device registered. Delivery still depends on phone permissions and push service availability.",
                              );
                            })
                          }
                        />
                        <Button
                          muted
                          label="Disable alerts on this phone"
                          onPress={() =>
                            run(async () => {
                              await disablePhoneAlerts();
                              await refresh();
                              setNotice(
                                "Phone alerts disabled for this device",
                              );
                            })
                          }
                        />
                      </>
                    ) : (
                      <Text style={s.note}>
                        Install the signed mobile app to enable phone
                        notifications. This browser shows attention warnings
                        while open.
                      </Text>
                    )}
                  </Card>
                  <Card title="Connections">
                    <Text style={s.note}>
                      Kotak Neo supported · Dhan coming later. Broker
                      verification may be required again if the broker rejects
                      the saved session.
                    </Text>
                    <Text style={s.note}>
                      Telegram:{" "}
                      {connection.telegram?.linked
                        ? "Linked"
                        : connection.step || "Not linked"}{" "}
                      · Kotak last verified:{" "}
                      {when(connection.kotak?.checked_at)}
                    </Text>
                    <Button
                      muted
                      label="Pause to configure"
                      onPress={() =>
                        run(async () => {
                          await api("/control", { enabled: false });
                          await refresh();
                          setNotice(
                            "Paused. Allow a few seconds for the worker to stop. Open positions keep their worker running.",
                          );
                        })
                      }
                    />
                  </Card>
                  <Card title="Telegram">
                    {field("telegram_api_id", "Telegram API ID")}
                    {field("telegram_api_hash", "Telegram API hash", true)}
                    <Button
                      label="Save Telegram credentials"
                      onPress={() =>
                        run(async () => {
                          await api(
                            "/credentials",
                            {
                              telegram_api_id: val("telegram_api_id"),
                              telegram_api_hash: val("telegram_api_hash"),
                            },
                            "PUT",
                          );
                          setForm((f) => ({ ...f, telegram_api_hash: "" }));
                          setNotice("Saved");
                        })
                      }
                    />
                    {field("phone", "Phone number with country code")}
                    <Button
                      muted
                      label="Send login code"
                      onPress={() =>
                        run(() =>
                          connect("telegram/start", { phone: val("phone") }),
                        )
                      }
                    />
                    {field("code", "Telegram login code", true)}
                    <Button
                      muted
                      label="Verify code"
                      onPress={() =>
                        run(() =>
                          connect("telegram/code", { code: val("code") }),
                        )
                      }
                    />
                    {field(
                      "password",
                      "Telegram two-step password (if requested)",
                      true,
                    )}
                    <Button
                      muted
                      label="Verify password"
                      onPress={() =>
                        run(() =>
                          connect("telegram/password", {
                            password: val("password"),
                          }),
                        )
                      }
                    />
                  </Card>
                  <Card title="Kotak Neo">
                    {field("kotak_consumer_key", "Consumer token", true)}
                    {field("kotak_mobile", "Registered mobile")}
                    {field("kotak_ucc", "Client code")}
                    {field("kotak_mpin", "MPIN", true)}
                    <Button
                      label="Save Kotak credentials"
                      onPress={() =>
                        run(async () => {
                          await api(
                            "/credentials",
                            Object.fromEntries(
                              [
                                "kotak_consumer_key",
                                "kotak_mobile",
                                "kotak_ucc",
                                "kotak_mpin",
                              ].map((k) => [k, val(k)]),
                            ),
                            "PUT",
                          );
                          setForm((f) => ({
                            ...f,
                            kotak_consumer_key: "",
                            kotak_mpin: "",
                          }));
                          setNotice("Saved");
                        })
                      }
                    />
                    {field("totp", "Current six-digit TOTP", true)}
                    <Button
                      muted
                      label="Verify Kotak connection"
                      onPress={() =>
                        run(() =>
                          connect("kotak/verify", { totp: val("totp") }),
                        )
                      }
                    />
                  </Card>
                  <Card title="Paper risk limits">
                    <Text style={s.note}>
                      Pause entries and finish open trades before editing. Risk
                      limits can reject signals that need more than your budget.
                    </Text>
                    {[
                      ["risk_per_trade", "Risk per trade ₹"],
                      ["daily_loss_limit", "Daily realized loss limit ₹"],
                      ["max_open_risk", "Total open risk ₹"],
                      ["max_lots", "Maximum lots"],
                      ["max_open_positions", "Maximum open positions"],
                      ["max_entries_per_day", "Entries per day"],
                    ].map(([k, l]) => field(k, l))}
                    <Button
                      label="Save risk limits"
                      onPress={() =>
                        run(async () => {
                          await api(
                            "/risk",
                            Object.fromEntries(
                              [
                                "risk_per_trade",
                                "daily_loss_limit",
                                "max_open_risk",
                                "max_lots",
                                "max_open_positions",
                                "max_entries_per_day",
                              ].map((k) => [k, Number(val(k))]),
                            ),
                            "PUT",
                          );
                          await refresh();
                          setNotice("Risk limits saved");
                        })
                      }
                    />
                  </Card>
                  <Card title="Adjust virtual cash">
                    {field("balance", "New available paper cash ₹")}
                    {field("reason", "Reason for adjustment")}
                    <Button
                      muted
                      label="Update paper balance"
                      onPress={() =>
                        run(async () => {
                          if (!val("balance").trim())
                            throw new Error("Enter the new paper balance");
                          await api("/paper-balance", {
                            amount: Number(val("balance")),
                            reason: val("reason"),
                          });
                          await refresh();
                          setNotice("Paper balance updated");
                        })
                      }
                    />
                  </Card>
                </>
              )}
              {!selected && tab === "Owner" && (
                <>
                  <OwnerSummary
                    users={users}
                    busy={busy}
                    onUser={(id) =>
                      run(async () => {
                        setSelected(await api("/admin/users/" + id));
                        setEvents([]);
                      })
                    }
                    onAccess={(id, access) =>
                      run(async () => {
                        await api(
                          "/admin/users/" + id + "/access",
                          { access },
                          "PUT",
                        );
                        const d = await api("/admin/users");
                        setUsers(d.users);
                        setAdminAudit(d.audit || []);
                      })
                    }
                  />
                  <Card title="Recent admin activity">
                    {adminAudit.length === 0 && (
                      <Text style={s.note}>
                        No admin activity recorded yet.
                      </Text>
                    )}
                    {adminAudit.slice(0, 5).map((e: Obj, i: number) => (
                      <View key={i} style={{ gap: 5 }}>
                        <Text style={s.text}>
                          {(
                            e.event ||
                            e.action ||
                            "Account updated"
                          ).replaceAll("_", " ")}
                        </Text>
                        <Text style={s.note}>
                          {new Date(Number(e.at) * 1000).toLocaleString()}
                        </Text>
                      </View>
                    ))}
                  </Card>
                  <Text style={s.cardTitle}>Users & paper performance</Text>
                  {users.map((u) => (
                    <Pressable
                      key={u.id}
                      onPress={() =>
                        run(async () => {
                          setSelected(await api("/admin/users/" + u.id));
                          setEvents([]);
                        })
                      }
                    >
                      <Card title={u.username}>
                        <Text style={s.tag}>
                          {u.access.toUpperCase()} ·{" "}
                          {u.worker_online ? "ONLINE" : "OFFLINE"}
                        </Text>
                        <Metrics p={u.pnl} />
                        <Text style={s.note}>
                          View trades, signal decisions and permissions →
                        </Text>
                      </Card>
                    </Pressable>
                  ))}
                </>
              )}
              {(tab === "Activity" || selected) &&
                events.map((e, i) => (
                  <Card key={e.seq || i}>
                    <Text style={s.text}>{e.event?.replaceAll("_", " ")}</Text>
                    <Text style={s.note}>
                      {when(e.at)} · {e.signal_id || ""}
                    </Text>
                    <Text style={s.note}>{e.reason || e.detail || ""}</Text>
                  </Card>
                ))}
              {(tab === "Activity" || selected) && cursor && (
                <Button
                  muted
                  label="Load older activity"
                  onPress={() => run(() => loadActivity(selected?.id, cursor))}
                />
              )}
            </>
          )}
        </ScrollView>
        {me?.access === "approved" && (
          <View style={s.nav}>
            {[
              "Home",
              "Channels",
              "Trades",
              "Account",
              ...(me.role === "owner" ? ["Owner"] : []),
            ].map((t, i) => (
              <Pressable
                key={t}
                accessibilityRole="tab"
                accessibilityState={{ selected: tab === t }}
                onPress={() => run(() => navigate(t))}
                style={s.navItem}
              >
                <Ionicons
                  name={
                    (
                      [
                        "grid-outline",
                        "radio-outline",
                        "swap-horizontal-outline",
                        "person-outline",
                        "shield-checkmark-outline",
                      ] as const
                    )[i]
                  }
                  color={tab === t ? "#55e4c5" : "#7b90aa"}
                  size={21}
                />
                <Text style={[s.navText, tab === t && { color: "#55e4c5" }]}>
                  {t}
                </Text>
              </Pressable>
            ))}
          </View>
        )}
      </View>
    </SafeAreaView>
  );
}
const s = StyleSheet.create({
  safe: { flex: 1, backgroundColor: "#041323" },
  shell: { flex: 1, width: "100%", maxWidth: 920, alignSelf: "center" },
  header: {
    padding: 22,
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    borderBottomWidth: 1,
    borderColor: "#17273b",
  },
  brand: { fontSize: 22, fontWeight: "800", color: "#eef5ff" },
  content: { padding: 20, gap: 18, paddingBottom: 35 },
  title: { fontSize: 28, fontWeight: "700", color: "#eef5ff", flexShrink: 1 },
  hero: { borderRadius: 24, padding: 24, gap: 20 },
  eyebrow: {
    color: "#78c9be",
    fontSize: 11,
    letterSpacing: 1.5,
    fontWeight: "700",
  },
  balance: { fontSize: 38, color: "#f1fffc", fontWeight: "700" },
  card: {
    backgroundColor: "#10243b",
    borderRadius: 20,
    padding: 20,
    gap: 15,
    borderWidth: 1,
    borderColor: "#29435f",
  },
  cardTitle: { fontSize: 18, color: "#e6effa", fontWeight: "600" },
  text: { color: "#d1dfef", fontSize: 14, lineHeight: 22 },
  note: { color: "#8fa3bb", fontSize: 12, lineHeight: 19 },
  label: { color: "#a9bacf", fontSize: 12 },
  tag: { color: "#55e4c5", fontSize: 11, fontWeight: "600" },
  row: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 12,
    flexWrap: "wrap",
  },
  wrap: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  metrics: { flexDirection: "row", gap: 12 },
  metric: { flex: 1, gap: 8 },
  value: { fontSize: 20, color: "#64e3bd", fontWeight: "600" },
  button: {
    backgroundColor: "#55e4c5",
    paddingHorizontal: 16,
    paddingVertical: 14,
    borderRadius: 12,
    alignItems: "center",
  },
  muted: { backgroundColor: "#122d47", borderWidth: 1, borderColor: "#446b91" },
  buttonText: { color: "#08251f", fontWeight: "700", fontSize: 13 },
  input: {
    backgroundColor: "#081423",
    borderWidth: 1,
    borderColor: "#2a405a",
    borderRadius: 12,
    padding: 14,
    fontSize: 16,
    color: "#eff7ff",
  },
  alert: { backgroundColor: "#4c2532", padding: 15, borderRadius: 14 },
  mode: {
    flexDirection: "row",
    gap: 12,
    backgroundColor: "#122137",
    padding: 10,
    borderRadius: 10,
  },
  nav: {
    flexDirection: "row",
    paddingVertical: 14,
    borderTopWidth: 1,
    borderColor: "#203047",
    backgroundColor: "#0a1627",
  },
  navItem: { flex: 1, alignItems: "center", gap: 5 },
  navText: { fontSize: 10, color: "#7b90aa" },
});
