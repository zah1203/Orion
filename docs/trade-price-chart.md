# Paper trade price chart

The trade detail screen plots retained broker observations for the position's
exact exchange segment and token. The header retains product, expiry, strike and
PUT/CALL identity. It does not require manually finding the contract in a broker
app. LTP is the default series; Bid matches the price basis used for paper exit
valuation. The chart does not submit orders or modify recorded trades.

1D, 1W and 1M mean trailing 24 hours, seven days and thirty days ending now, or
at the recorded final exit for a closed trade. Coverage never starts before the
signal identified this contract. This is retained observation history, not a
Kotak historical-candle integration or a guarantee of complete exchange data.
Telegram price messages are excluded. Missing data shows an empty state; gaps
over 60 seconds break the line. Valid open-market observations must also be
fresh at receipt under the account's quote-age setting.

The read-only query loads at most the newest 8,000 raw quote rows for the window;
when capped, the UI warns that coverage is limited. At most the most recent 240
continuous segments are shown. Sampling preserves endpoints and observed bid/LTP
extrema within buckets, without connecting distinct segments. The chart can be
sparser than the broker app and cannot establish an unobserved target crossing.

Entry, current stop and targets are horizontal guides, with recorded entry and
exit fills marked B/S. The current-stop guide is explicitly not a reconstruction
of historical stop movements. A toggle hides guides to zoom into observed price
movement. Tap the chart to inspect the nearest retained quote's time, bid and LTP.
All chart timestamps are displayed in IST.

The chart shares the trade page's ten-second refresh. Existing owner/account
authorization applies to its `chart_window=1D|1W|1M` parameter. A new Expo-compatible
react-native-svg dependency is included in package.json and package-lock.json.
The web dashboard is rebuilt by deployment; native clients require a build with
the compatible SVG module (also included in Expo Go).
