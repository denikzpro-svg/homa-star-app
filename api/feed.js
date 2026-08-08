export default function handler(req, res) {
  // Список фейковых участников и сумм (хранится ТОЛЬКО на сервере Vercel)
  const names = [
    "Alex_Pro", "Dmitry_K", "Максим", "Артем_99", 
    "Sasha_G", "Виктория", "Nikita_Star", "Den_King"
  ];
  const actions = [
    "выиграл в Блиц-Турнире", 
    "получил за победу", 
    "забрал из сундука", 
    "вывел на кошелек"
  ];

  const randomName = names[Math.floor(Math.random() * names.length)];
  const randomAction = actions[Math.floor(Math.random() * actions.length)];
  const randomStars = Math.floor(Math.random() * 45) + 5; // от 5 до 50 звезд

  // Отдаем клиенту только результат
  res.status(200).json({
    user: randomName,
    action: randomAction,
    stars: randomStars,
    time: "Только что"
  });
}
