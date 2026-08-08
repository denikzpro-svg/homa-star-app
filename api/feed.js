export default function handler(req, res) {
  const liveWins = [
    "⭐ <b>@crypto_king</b> выиграл +25 ⭐",
    "👑 <b>@homa_lord</b> сорвал куш 200 ⭐",
    "🔥 <b>@sasha_777</b> забрал +60 ⭐",
    "⭐ <b>@elon_homyak</b> открыл Королевский кейс",
    "💎 <b>@dark_1999</b> выбил +150 ⭐",
    "⚡ <b>@bogdan_tG</b> выиграл +40 ⭐",
    "🔥 <b>@den_king</b> затащил Блиц-Турнир",
    "🏆 <b>@star_master</b> вывел 100 ⭐"
  ];

  const randomWin = liveWins[Math.floor(Math.random() * liveWins.length)];

  res.status(200).json({ text: randomWin });
}
