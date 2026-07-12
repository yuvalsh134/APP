import React, { useState, useEffect, useCallback, useMemo } from "react";
import {
  View,
  Text,
  StyleSheet,
  FlatList,
  TextInput,
  TouchableOpacity,
  ScrollView,
  RefreshControl,
  ActivityIndicator,
  SafeAreaView,
  StatusBar,
} from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { useFonts, Fraunces_600SemiBold, Fraunces_700Bold, Fraunces_500Medium_Italic } from "@expo-google-fonts/fraunces";
import { IBMPlexMono_400Regular, IBMPlexMono_500Medium, IBMPlexMono_600SemiBold } from "@expo-google-fonts/ibm-plex-mono";
import { DATA_URL } from "./config";

// ── palette: aged ledger paper, not another dark SaaS dashboard ──
const C = {
  paper: "#F2E9D8",
  paperShade: "#E9DCC1",
  card: "#F8F2E4",
  ink: "#211D14",
  inkSoft: "#6B6552",
  rule: "#C7B991",
  ruleLight: "#DCD0AE",
  brass: "#8A5A2B",
};

const CATEGORY_META = {
  "REVERSAL BUY":       { color: "#2F6B4F", mark: "R", desc: "היפוך מומנטום, הפרסום הפיננסי מתחזק" },
  "TREND BUY":          { color: "#1F4E79", mark: "T", desc: "מגמה רב-טווחית, לצד צמיחת הכנסות" },
  "GROWTH STOCKS":      { color: "#0E6E6E", mark: "G", desc: "צמיחה גבוהה ועקבית לאורך רבעונים" },
  "EMA200 SUPPORT":     { color: "#5B4B8A", mark: "E", desc: "החזקה מעל קו התמיכה 200" },
  "BUY RSI":            { color: "#A6741B", mark: "B", desc: "מומנטום RSI מאושר מעל המגמה" },
  "Counter-Trend BUY":  { color: "#6B5B2A", mark: "C", desc: "כניסה נגד-מגמה, גיבוי מוסדי" },
  "Counter-Trend SELL": { color: "#8C2F2F", mark: "S", desc: "קניית יתר, נתקע מתחת להתנגדות" },
};

function fmtCap(v) {
  if (v == null) return "—";
  if (v >= 1e12) return `$${(v / 1e12).toFixed(2)}T`;
  if (v >= 1e9) return `$${(v / 1e9).toFixed(2)}B`;
  if (v >= 1e6) return `$${(v / 1e6).toFixed(1)}M`;
  return `$${(v / 1e3).toFixed(0)}K`;
}
function rsColor(v) {
  if (v == null) return C.inkSoft;
  if (v >= 70) return "#2F6B4F";
  if (v >= 50) return "#A6741B";
  return "#8C2F2F";
}
function athColor(v) {
  if (v == null) return C.inkSoft;
  if (v > 50) return "#8C2F2F";
  if (v > 30) return "#A6741B";
  return C.inkSoft;
}

export default function App() {
  const [fontsLoaded] = useFonts({
    Fraunces_600SemiBold, Fraunces_700Bold, Fraunces_500Medium_Italic,
    IBMPlexMono_400Regular, IBMPlexMono_500Medium, IBMPlexMono_600SemiBold,
  });

  const [raw, setRaw] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState(null);
  const [activeCat, setActiveCat] = useState(null);
  const [query, setQuery] = useState("");

  const load = useCallback(async () => {
    try {
      setError(null);
      const res = await fetch(`${DATA_URL}?t=${Date.now()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setRaw(json);
    } catch (e) {
      setError("לא הצלחתי לטעון נתונים. בדוק שה-URL ב-config.js נכון וש-GitHub Pages פעיל.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  const onRefresh = () => { setRefreshing(true); load(); };

  const categories = useMemo(() => {
    if (!raw) return [];
    const map = {};
    raw.alerts.forEach((a) => {
      if (!map[a.category]) map[a.category] = [];
      map[a.category].push(a);
    });
    return Object.keys(CATEGORY_META)
      .filter((c) => map[c] && map[c].length)
      .map((c) => ({ name: c, count: map[c].length, items: map[c] }));
  }, [raw]);

  const cat = activeCat || (categories[0] && categories[0].name);

  const filtered = useMemo(() => {
    const found = categories.find((c) => c.name === cat);
    let items = found ? found.items : [];
    if (query.trim()) {
      const q = query.trim().toUpperCase();
      items = items.filter((i) => i.ticker.toUpperCase().includes(q));
    }
    return [...items].sort((a, b) => (b.rs_rating ?? -1) - (a.rs_rating ?? -1));
  }, [categories, cat, query]);

  if (!fontsLoaded || loading) {
    return (
      <SafeAreaView style={styles.root}>
        <StatusBar barStyle="dark-content" backgroundColor={C.paper} />
        <View style={styles.centerFill}>
          <ActivityIndicator color={C.brass} size="large" />
          {fontsLoaded && <Text style={styles.mutedText}>טוען את הגיליון האחרון…</Text>}
        </View>
      </SafeAreaView>
    );
  }

  if (error || !raw) {
    return (
      <SafeAreaView style={styles.root}>
        <StatusBar barStyle="dark-content" backgroundColor={C.paper} />
        <View style={styles.centerFill}>
          <Ionicons name="alert-circle-outline" size={30} color="#8C2F2F" />
          <Text style={[styles.mutedText, { textAlign: "center", marginTop: 10 }]}>{error}</Text>
          <TouchableOpacity style={styles.retryBtn} onPress={load}>
            <Text style={styles.retryBtnText}>נסה שוב</Text>
          </TouchableOpacity>
        </View>
      </SafeAreaView>
    );
  }

  const totalCount = raw.alerts.length;
  const dateline = new Date(raw.scan_time)
    .toLocaleDateString("he-IL", { day: "2-digit", month: "long", year: "numeric" })
    .toUpperCase();
  const timeline = new Date(raw.scan_time).toLocaleTimeString("he-IL", { hour: "2-digit", minute: "2-digit" });

  return (
    <SafeAreaView style={styles.root}>
      <StatusBar barStyle="dark-content" backgroundColor={C.paper} />

      {/* ── masthead ── */}
      <View style={styles.masthead}>
        <Text style={styles.mastheadKicker}>גיליון סריקה יומי</Text>
        <Text style={styles.mastheadTitle}>Screening Desk</Text>
        <View style={styles.ruleDouble}>
          <View style={styles.ruleThick} />
          <View style={styles.ruleThin} />
        </View>
        <View style={styles.mastheadRow}>
          <Text style={styles.dateline}>{dateline} · {timeline}</Text>
        </View>
      </View>

      {/* ── ledger stats ── */}
      <View style={styles.ledgerRow}>
        <LedgerStat num={totalCount} label="מסומנות" />
        <View style={styles.vRule} />
        <LedgerStat num={raw.stats?.total_scanned ?? "—"} label="נסרקו" />
        <View style={styles.vRule} />
        <LedgerStat num={categories.length} label="אסטרטגיות" />
      </View>

      {/* ── category tabs ── */}
      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        style={styles.tabRow}
        contentContainerStyle={{ paddingHorizontal: 16 }}
      >
        {categories.map((c) => {
          const meta = CATEGORY_META[c.name];
          const isActive = cat === c.name;
          return (
            <TouchableOpacity
              key={c.name}
              style={[styles.tab, isActive && styles.tabActive, { borderTopColor: meta.color }]}
              onPress={() => { setActiveCat(c.name); setQuery(""); }}
            >
              <Text style={[styles.tabText, isActive && { color: C.ink }]} numberOfLines={1}>
                {c.name}
              </Text>
              <Text style={styles.tabCount}>{c.count}</Text>
            </TouchableOpacity>
          );
        })}
      </ScrollView>

      {cat && <Text style={styles.catDesc}>“{CATEGORY_META[cat]?.desc}”</Text>}

      <View style={styles.searchWrap}>
        <Ionicons name="search" size={13} color={C.inkSoft} />
        <TextInput
          style={styles.searchInput}
          placeholder="חיפוש טיקר…"
          placeholderTextColor={C.inkSoft}
          value={query}
          onChangeText={setQuery}
        />
        {query.length > 0 && (
          <TouchableOpacity onPress={() => setQuery("")}>
            <Ionicons name="close" size={13} color={C.inkSoft} />
          </TouchableOpacity>
        )}
      </View>

      <FlatList
        data={filtered}
        keyExtractor={(item) => item.ticker}
        contentContainerStyle={{ padding: 16, paddingBottom: 40 }}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={C.brass} />}
        ListEmptyComponent={<Text style={[styles.mutedText, { textAlign: "center", marginTop: 30 }]}>אין תוצאות בעמודה הזו</Text>}
        renderItem={({ item }) => <TickerTicket item={item} meta={CATEGORY_META[cat]} />}
      />
    </SafeAreaView>
  );
}

function LedgerStat({ num, label }) {
  return (
    <View style={styles.ledgerStat}>
      <Text style={styles.ledgerNum}>{num}</Text>
      <Text style={styles.ledgerLabel}>{label}</Text>
    </View>
  );
}

function TickerTicket({ item, meta }) {
  const above = item.vwap == null || item.price > item.vwap;
  return (
    <View style={styles.ticket}>
      <View style={styles.ticketTopRow}>
        <View style={styles.ticketTitleWrap}>
          <View style={[styles.seal, { borderColor: meta?.color || C.rule }]}>
            <Text style={[styles.sealText, { color: meta?.color || C.inkSoft }]}>{meta?.mark || "•"}</Text>
          </View>
          <Text style={styles.ticketTicker}>{item.ticker}</Text>
        </View>
        <Text style={styles.ticketPrice}>${item.price?.toFixed(2)}</Text>
      </View>

      <View style={styles.ticketHairline} />

      <View style={styles.ticketRow}>
        <Metric label="שווי שוק" value={fmtCap(item.market_cap)} />
        <View style={styles.metricRule} />
        <Metric label="דירוג RS" value={item.rs_rating != null ? Math.round(item.rs_rating) : "—"} color={rsColor(item.rs_rating)} />
        <View style={styles.metricRule} />
        <Metric label="מרחק משיא" value={item.ath_dist != null ? `${item.ath_dist.toFixed(1)}%` : "—"} color={athColor(item.ath_dist)} />
      </View>

      <View style={styles.ticketRow}>
        <Metric
          label="צמיחת הכנסות"
          value={item.rev_growth != null ? `${item.rev_growth > 0 ? "+" : ""}${item.rev_growth.toFixed(1)}%` : "—"}
          color={item.rev_growth == null ? C.inkSoft : item.rev_growth > 0 ? "#2F6B4F" : "#8C2F2F"}
        />
        <View style={styles.metricRule} />
        <Metric label="P/E" value={item.pe != null ? item.pe.toFixed(1) : "—"} />
        <View style={styles.metricRule} />
        <Metric label="ROE" value={item.roe != null ? `${(item.roe * 100).toFixed(1)}%` : "—"} />
      </View>

      <View style={styles.stampRow}>
        <Stamp label="VWAP" on={above} />
        <Stamp label="2LS" on={item.two_lower_shadow} />
        <Stamp label="HVSC" on={item.high_vol_strong_close} />
      </View>
    </View>
  );
}

function Metric({ label, value, color }) {
  return (
    <View style={{ flex: 1 }}>
      <Text style={styles.metricLabel}>{label}</Text>
      <Text style={[styles.metricValue, color && { color }]}>{value}</Text>
    </View>
  );
}

function Stamp({ label, on }) {
  return (
    <View style={[styles.stamp, on && styles.stampOn]}>
      <Text style={[styles.stampText, on && styles.stampTextOn]}>{label}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: C.paper },
  centerFill: { flex: 1, alignItems: "center", justifyContent: "center", padding: 24 },
  mutedText: { color: C.inkSoft, marginTop: 10, fontSize: 13, fontFamily: "IBMPlexMono_400Regular" },
  retryBtn: { marginTop: 16, borderWidth: 1, borderColor: C.brass, paddingHorizontal: 16, paddingVertical: 8 },
  retryBtnText: { color: C.brass, fontSize: 12.5, fontFamily: "IBMPlexMono_600SemiBold", letterSpacing: 0.5 },

  masthead: { paddingHorizontal: 20, paddingTop: 14, alignItems: "center" },
  mastheadKicker: { fontFamily: "IBMPlexMono_500Medium", fontSize: 10, letterSpacing: 3, color: C.brass, textTransform: "uppercase" },
  mastheadTitle: { fontFamily: "Fraunces_700Bold", fontSize: 32, color: C.ink, marginTop: 4 },
  ruleDouble: { width: "100%", marginTop: 10, alignItems: "stretch" },
  ruleThick: { height: 2, backgroundColor: C.ink },
  ruleThin: { height: 1, backgroundColor: C.ink, marginTop: 2 },
  mastheadRow: { marginTop: 8, marginBottom: 4 },
  dateline: { fontFamily: "IBMPlexMono_400Regular", fontSize: 10.5, color: C.inkSoft, letterSpacing: 1 },

  ledgerRow: { flexDirection: "row", alignItems: "center", justifyContent: "center", paddingVertical: 14, borderBottomWidth: 1, borderBottomColor: C.rule, backgroundColor: C.paperShade },
  ledgerStat: { alignItems: "center", paddingHorizontal: 22 },
  vRule: { width: 1, height: 26, backgroundColor: C.rule },
  ledgerNum: { fontFamily: "IBMPlexMono_600SemiBold", fontSize: 19, color: C.ink },
  ledgerLabel: { fontFamily: "IBMPlexMono_400Regular", fontSize: 9.5, color: C.inkSoft, letterSpacing: 0.6, marginTop: 2 },

  tabRow: { marginTop: 14, flexGrow: 0 },
  tab: { borderTopWidth: 3, borderColor: "transparent", paddingHorizontal: 12, paddingVertical: 9, marginRight: 4, backgroundColor: C.paperShade, minWidth: 92 },
  tabActive: { backgroundColor: C.card },
  tabText: { fontFamily: "IBMPlexMono_500Medium", fontSize: 10, color: C.inkSoft, letterSpacing: 0.3 },
  tabCount: { fontFamily: "Fraunces_600SemiBold", fontSize: 15, color: C.ink, marginTop: 2 },

  catDesc: { fontFamily: "Fraunces_500Medium_Italic", fontSize: 13, color: C.inkSoft, paddingHorizontal: 20, marginTop: 12 },

  searchWrap: { flexDirection: "row", alignItems: "center", gap: 8, borderWidth: 1, borderColor: C.rule, marginHorizontal: 16, marginTop: 12, paddingHorizontal: 10, paddingVertical: 8, backgroundColor: C.card },
  searchInput: { flex: 1, color: C.ink, fontFamily: "IBMPlexMono_400Regular", fontSize: 12.5 },

  ticket: { backgroundColor: C.card, borderWidth: 1, borderColor: C.rule, padding: 14, marginBottom: 12 },
  ticketTopRow: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  ticketTitleWrap: { flexDirection: "row", alignItems: "center", gap: 8 },
  seal: { width: 24, height: 24, borderRadius: 12, borderWidth: 1.5, alignItems: "center", justifyContent: "center" },
  sealText: { fontFamily: "IBMPlexMono_600SemiBold", fontSize: 11 },
  ticketTicker: { fontFamily: "Fraunces_700Bold", fontSize: 19, color: C.ink },
  ticketPrice: { fontFamily: "IBMPlexMono_600SemiBold", fontSize: 15, color: C.ink },

  ticketHairline: { height: 1, backgroundColor: C.ruleLight, marginVertical: 10 },

  ticketRow: { flexDirection: "row", marginBottom: 10, alignItems: "flex-start" },
  metricRule: { width: 1, backgroundColor: C.ruleLight, marginHorizontal: 10 },
  metricLabel: { fontFamily: "IBMPlexMono_400Regular", fontSize: 9, color: C.inkSoft, textTransform: "uppercase", letterSpacing: 0.4 },
  metricValue: { fontFamily: "IBMPlexMono_600SemiBold", fontSize: 13, color: C.ink, marginTop: 3 },

  stampRow: { flexDirection: "row", gap: 8, marginTop: 2, paddingTop: 10, borderTopWidth: 1, borderTopColor: C.ruleLight },
  stamp: { borderWidth: 1, borderColor: C.ruleLight, paddingHorizontal: 8, paddingVertical: 3 },
  stampOn: { borderColor: C.brass, backgroundColor: "rgba(138,90,43,0.08)" },
  stampText: { fontFamily: "IBMPlexMono_500Medium", fontSize: 9.5, color: C.ruleLight, letterSpacing: 0.5 },
  stampTextOn: { color: C.brass },
});
