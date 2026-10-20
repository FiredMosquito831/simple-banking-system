// Danube Ops — admin-only interactions
(() => {
  // Slide-open panels (new customer, issue card, booking a loan, reversal)
  document.querySelectorAll('[data-toggle-panel]').forEach((btn) => {
    const panel = document.getElementById(btn.dataset.togglePanel);
    if (!panel) return;
    btn.addEventListener('click', () => {
      panel.hidden = !panel.hidden;
      if (!panel.hidden) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });

  // Reveal a card number for an operator (audited server-side)
  document.querySelectorAll('[data-reveal-card]').forEach((btn) => {
    const id = btn.dataset.revealCard;
    const target = document.querySelector(`[data-reveal-target="${id}"]`);
    const original = btn.textContent;
    btn.addEventListener('click', async () => {
      if (!target) return;
      btn.disabled = true;
      try {
        const res = await fetch(`/cards/${id}/reveal`, { headers: { Accept: 'application/json' } });
        if (!res.ok) throw new Error('Reveal failed');
        const data = await res.json();
        target.hidden = false;
        target.textContent = `${data.pan.replace(/(.{4})/g, '$1 ').trim()} · exp ${data.expiry}`;
        btn.textContent = 'Hide';
        setTimeout(() => {
          target.hidden = true;
          btn.textContent = original;
        }, 15000);
      } catch (err) {
        window.alert(err.message || 'Could not reveal card');
      } finally {
        btn.disabled = false;
      }
    });
  });
})();
