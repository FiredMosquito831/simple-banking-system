// Danube Bank demo — small progressive enhancements
(() => {
  const shell = document.querySelector('.shell');

  // Mobile sidebar
  document.querySelectorAll('[data-toggle-sidebar]').forEach((btn) => {
    btn.addEventListener('click', () => shell && shell.classList.toggle('nav-open'));
  });
  document.addEventListener('click', (e) => {
    if (!shell || !shell.classList.contains('nav-open')) return;
    if (e.target.closest('.sidebar') || e.target.closest('[data-toggle-sidebar]')) return;
    shell.classList.remove('nav-open');
  });

  // Confirm destructive submissions
  document.querySelectorAll('form[data-confirm-submit]').forEach((form) => {
    form.addEventListener('submit', (e) => {
      if (!window.confirm(form.dataset.confirmSubmit)) e.preventDefault();
    });
  });

  // Demo login: fill the email field from a chip
  document.querySelectorAll('[data-fill-email]').forEach((chip) => {
    chip.addEventListener('click', () => {
      const email = document.querySelector('input[name="email"]');
      const password = document.querySelector('input[name="password"]');
      if (email) {
        email.value = chip.dataset.fillEmail;
        email.dispatchEvent(new Event('input', { bubbles: true }));
      }
      if (password) password.focus();
    });
  });

  // Transfer helpers
  document.querySelectorAll('[data-fill-account]').forEach((chip) => {
    chip.addEventListener('click', () => {
      const input = document.getElementById('toAccount');
      if (input) {
        input.value = chip.dataset.fillAccount;
        input.dispatchEvent(new Event('input', { bubbles: true }));
      }
    });
  });
  document.querySelectorAll('[data-amount]').forEach((chip) => {
    chip.addEventListener('click', () => {
      const input = document.getElementById('amount');
      if (input) {
        input.value = chip.dataset.amount;
        input.focus();
      }
    });
  });

  // Reveal a card number (demo only)
  document.querySelectorAll('[data-reveal-card]').forEach((btn) => {
    const id = btn.dataset.revealCard;
    const target = document.querySelector(`[data-card-number="${id}"]`);
    let timer = null;
    btn.addEventListener('click', async () => {
      if (!target) return;
      if (timer) {
        clearTimeout(timer);
        timer = null;
        target.textContent = target.dataset.masked || target.textContent;
        return;
      }
      target.dataset.masked = target.textContent;
      btn.disabled = true;
      try {
        const res = await fetch(`/cards/${id}/details`, { headers: { Accept: 'application/json' } });
        if (!res.ok) throw new Error('Could not reveal card');
        const data = await res.json();
        target.textContent = data.pan.replace(/(.{4})/g, '$1 ').trim();
        timer = setTimeout(() => {
          target.textContent = target.dataset.masked;
          timer = null;
        }, 12000);
      } catch (err) {
        window.alert(err.message || 'Could not reveal card');
      } finally {
        btn.disabled = false;
      }
    });
  });

  // Auto-dismiss success toasts after a while
  document.querySelectorAll('.toast-success').forEach((toast) => {
    setTimeout(() => {
      toast.style.transition = 'opacity .4s ease';
      toast.style.opacity = '0';
      setTimeout(() => toast.remove(), 420);
    }, 6000);
  });

  // Format account number inputs as you type (DANB 4000 1234 5678 90)
  document.querySelectorAll('input[name="to_account_number"]').forEach((input) => {
    input.addEventListener('blur', () => {
      const raw = input.value.replace(/\s+/g, '').toUpperCase();
      if (/^DANB\d{14}$/.test(raw)) {
        input.value = raw.match(/.{1,4}/g).join(' ');
      }
    });
  });
})();
