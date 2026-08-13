let userBalance = 100; // Для теста ставим 100
let isSpinning = false;
let currentWheelRotation = 0;
let userSpinCounts = JSON.parse(localStorage.getItem('homastar_spins') || '{}');

// НАСТРОЙКА СЕКТОРОВ НА БАРАБАНЕ
const WHEEL_CONFIGS = {
    // Бесплатное: нарисовано 100, 50, 10, 5, 1
    free: { 
        price: 0, 
        prizes: [100, 50, 10, 5, 1, 100, 50, 10, 5, 1], 
        icons: ['💎', '👑', '💰', '✨', '⭐', '💎', '👑', '💰', '✨', '⭐'] 
    },
    // Платное: разные варианты
    common: { 
        price: 20, 
        prizes: [500, 100, 50, 30, 25, 20, 15, 10, 0, 0], 
        icons: ['💎', '✨', '💰', '💰', '⭐', '⭐', '⭐', '⭐', '❌', '❌'] 
    }
};

// Функция переключения экранов
function openScreen(screenId) {
    document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
    document.getElementById(screenId).classList.add('active');
}

// Закрыть модалку
function closeModal(id) { document.getElementById(id).style.display = 'none'; }

// МАТЕМАТИКА КОЭФФИЦИЕНТОВ
function calculatePrizeIndex(type) {
    const count = userSpinCounts[type] || 0; 
    userSpinCounts[type] = count + 1; 
    localStorage.setItem('homastar_spins', JSON.stringify(userSpinCounts)); 
    
    const rand = Math.random() * 100;

    // ПЛАТНОЕ КОЛЕСО
    if (type === 'common') {
        // Первый спин - гарантированный куш (сектор 50 Stars, индекс 2)
        if (count === 0) return 2; 

        // Акулий режим (слив баланса)
        if (rand < 10) return 8; // Выпадет 0
        if (rand < 45) return 7; // Выпадет 10 (Слив)
        if (rand < 80) return 6; // Выпадет 15 (Слив)
        if (rand < 90) return 5; // Выпадет 20 (В ноль)
        if (rand < 95) return 4; // Выпадет 25
        return 3; // Выпадет 30
    }
    return 0;
}

// ЗАПУСК КОЛЕСА
function startSpin(type) {
    if (isSpinning) return; 
    const config = WHEEL_CONFIGS[type];
    
    if (type !== 'free') {
        if (userBalance < config.price) {
            alert("Недостаточно Stars!");
            return;
        }
        userBalance -= config.price; 
        document.getElementById('user-balance-text').innerText = userBalance + " ⭐";
    }
    
    isSpinning = true; 
    document.getElementById('btn-spin-action').classList.add('disabled');
    
    let prizeIndex;
    
    // БЕСПЛАТНОЕ КОЛЕСО: Всегда выпадает "1 Star" (индексы 4 или 9)
    if (type === 'free') {
        prizeIndex = Math.random() > 0.5 ? 4 : 9; 
    } else {
        // Платное колесо: берем логику из функции выше
        prizeIndex = calculatePrizeIndex(type); 
    }
    
    const prizeValue = config.prizes[prizeIndex];
    const arc = 360 / 10; 
    const targetAngle = 360 - (prizeIndex + 0.5) * arc;
    const currentModulo = currentWheelRotation % 360; 
    let distance = targetAngle - currentModulo; 
    if (distance <= 0) distance += 360;
    
    // Делаем 10 полных оборотов для 10-секундной анимации
    currentWheelRotation += distance + (10 * 360); 
    document.getElementById('wheel-canvas').style.transform = `rotate(${currentWheelRotation}deg)`;
    
    // Ждём ровно 10 секунд (10000 миллисекунд), пока крутится CSS
    setTimeout(() => {
        isSpinning = false; 
        userBalance += prizeValue; 
        document.getElementById('user-balance-text').innerText = userBalance + " ⭐";
        
        // Показываем выигрыш
        document.getElementById('win-modal-amount').innerText = "+" + prizeValue + " ⭐";
        document.getElementById('modal-win').style.display = 'flex';
        
        document.getElementById('btn-spin-action').classList.remove('disabled');
    }, 10000);
}

// Отрисовка визуала колеса (чтобы сектора были ровными)
function renderWheelDisc(type) {
    const canvas = document.getElementById('wheel-canvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const config = WHEEL_CONFIGS[type]; 
    const radius = canvas.width / 2;
    const arc = (2 * Math.PI) / 10;
    
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    for (let i = 0; i < 10; i++) {
        const angle = i * arc - Math.PI / 2;
        ctx.beginPath(); 
        ctx.fillStyle = i % 2 === 0 ? 'rgba(0, 243, 255, 0.2)' : 'rgba(255, 46, 147, 0.2)'; 
        ctx.moveTo(radius, radius); 
        ctx.arc(radius, radius, radius - 20, angle, angle + arc); 
        ctx.lineTo(radius, radius); 
        ctx.fill();
        
        ctx.save(); ctx.translate(radius, radius); ctx.rotate(angle + arc / 2);
        ctx.fillStyle = '#FFF'; ctx.font = '20px sans-serif'; ctx.textAlign = 'right';
        ctx.fillText(config.prizes[i] > 0 ? `+${config.prizes[i]}` : '0', radius - 60, 0);
        ctx.restore();
    }
}

// Подготовка экранов
function openPlayWheel(type) {
    openScreen('screen-game-wheel');
    renderWheelDisc(type);
    const btn = document.getElementById('btn-spin-action');
    btn.onclick = () => startSpin(type);
}

function openFreeWheelScreen() {
    openPlayWheel('free');
}
