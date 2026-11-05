import { post } from '../api.js';
import { h, btn, field, toast } from '../ui.js';

export default async function (root) {
  const cur = h('input', { type: 'password', autocomplete: 'current-password', required: true });
  const nw = h('input', { type: 'password', autocomplete: 'new-password', required: true, minlength: 8, maxlength: 256 });
  const conf = h('input', { type: 'password', autocomplete: 'new-password', required: true });
  const err = h('div', { class: 'form-err' });
  const submit = btn('Change password', { kind: 'primary', type: 'submit' });
  const form = h('form', { class: 'card settings-card', onSubmit: async e => {
    e.preventDefault(); err.textContent = '';
    if (nw.value.length < 8) { err.textContent = 'The new password must be at least 8 characters.'; return nw.focus(); }
    if (nw.value !== conf.value) { err.textContent = 'The new passwords do not match.'; return conf.focus(); }
    submit.disabled = true;
    try {
      await post('/password', { current: cur.value, new: nw.value }, { noAuthRedirect: true });
      form.reset(); toast('Admin password changed.', 'ok');
    } catch (x) {
      if (x.status === 409) err.textContent = 'Password is set via ADMIN_PASSWORD environment variable. Change the variable and restart the container.';
      else if (x.status === 403 || x.status === 401) { err.textContent = 'Current password is incorrect.'; cur.select(); }
      else if (x.status === 422) err.textContent = 'The new password must be between 8 and 256 characters.';
      else err.textContent = x.message;
    } finally { submit.disabled = false; }
  } },
    h('h3', null, 'Change admin password'),
    field('Current password', cur), field('New password', nw, 'At least 8 characters.'), field('Confirm new password', conf),
    err, h('div', { class: 'row' }, submit));
  root.append(h('div', { class: 'page-h' }, h('h1', null, 'Settings'), h('p', { class: 'muted' }, 'Administration settings.')), form);
}
