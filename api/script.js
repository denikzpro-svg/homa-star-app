function nextSlide(slideNumber) {
    document.querySelectorAll('.slide').forEach(slide => {
        slide.classList.remove('active');
    });

    const targetSlide = document.getElementById(`slide-${slideNumber}`);
    if (targetSlide) {
        targetSlide.classList.add('active');
    }
}

function claimGift() {
    const modal = document.getElementById('onboarding-modal');
    modal.style.display = 'none';
}

if (window.Telegram && window.Telegram.WebApp) {
    window.Telegram.WebApp.ready();
    window.Telegram.WebApp.expand();
}
