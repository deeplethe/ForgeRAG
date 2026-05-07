/**
 * Dialog + Toast system — replaces native confirm() / alert() /
 * prompt().
 *
 * Singleton reactive state shared app-wide. <DialogHost> (mounted once in
 * App.vue) renders both the modal dialog AND toast list. Any component
 * gets at the API via `useDialog()`.
 *
 * API:
 *
 *   const { confirm, alert, prompt, toast } = useDialog()
 *
 *   // Confirm — returns Promise<boolean>; resolves true on Continue.
 *   if (await confirm({
 *     title: 'Revoke token?',
 *     description: 'Clients using this SK will start getting 401.',
 *     confirmText: 'Revoke',
 *     variant: 'destructive',
 *   })) { ... }
 *
 *   // Alert — blocking informational modal, single OK button.
 *   await alert({ title: 'Heads up', description: 'Something happened.' })
 *
 *   // Prompt — blocking modal with a text input. Resolves to the
 *   // trimmed string on Save, or null on Cancel / empty.
 *   const next = await prompt({
 *     title: 'Rename conversation',
 *     description: 'Pick a new title.',
 *     placeholder: 'Title',
 *     initialValue: current,
 *     confirmText: 'Save',
 *   })
 *   if (next != null) ...
 *
 *   // Toast — transient non-blocking notification.
 *   toast('Saved.')                                // info
 *   toast('Upload failed', { variant: 'error' })   // red
 *   toast('Done', { variant: 'success' })          // green
 */

import { reactive } from 'vue'

const _dialog = reactive({
  open: false,
  type: 'confirm',          // 'confirm' | 'alert' | 'prompt'
  variant: 'default',       // 'default' | 'destructive'
  title: '',
  description: '',
  confirmText: 'Continue',
  cancelText: 'Cancel',
  // Prompt-only: text input state. ``inputValue`` is two-way
  // bound by DialogHost when ``type === 'prompt'``. Empty string
  // is treated as "no change" on submit and resolves to null.
  inputValue: '',
  inputPlaceholder: '',
  _resolve: null,
})

const _toasts = reactive([])
let _toastSeq = 0

function _openDialog(opts) {
  return new Promise((resolve) => {
    _dialog.open = true
    _dialog.type = opts.type || 'confirm'
    _dialog.variant = opts.variant || 'default'
    _dialog.title = opts.title || ''
    _dialog.description = opts.description || ''
    _dialog.confirmText = opts.confirmText || (opts.type === 'alert' ? 'OK' : 'Continue')
    _dialog.cancelText = opts.cancelText || 'Cancel'
    _dialog.inputValue = opts.initialValue || ''
    _dialog.inputPlaceholder = opts.placeholder || ''
    _dialog._resolve = resolve
  })
}

function _closeDialog(value) {
  // For prompt: ``confirm`` resolves with the trimmed value
  // (or null when blank / unchanged), ``cancel`` resolves null.
  // ``DialogHost`` passes the right shape via its onConfirm /
  // onCancel handlers; the dialog state itself doesn't need to
  // know — it just forwards whatever the host gives.
  _dialog.open = false
  if (_dialog._resolve) {
    _dialog._resolve(value)
    _dialog._resolve = null
  }
}

function _addToast(message, opts = {}) {
  const id = ++_toastSeq
  // Default TTL: action toasts get a longer window (8s) so the user can
  // actually click Undo. Loading toasts persist (ttl=0) until the caller
  // dismisses them — they represent an in-flight operation, not a status
  // update.
  let ttl
  if (opts.ttl != null) ttl = opts.ttl
  else if (opts.variant === 'loading') ttl = 0
  else if (opts.action) ttl = 8000
  else ttl = 4000

  const item = {
    id,
    message,
    variant: opts.variant || 'info', // 'info' | 'success' | 'error' | 'warn' | 'loading'
    action: opts.action || null,     // optional { label, onClick }
  }
  _toasts.push(item)
  if (ttl > 0) {
    setTimeout(() => _dismissToast(id), ttl)
  }
  return id
}

function _dismissToast(id) {
  const i = _toasts.findIndex((t) => t.id === id)
  if (i >= 0) _toasts.splice(i, 1)
}

export function useDialog() {
  return {
    // reactive state for <DialogHost> binding only; consumers shouldn't
    // mutate these directly.
    _dialogState: _dialog,
    _toastList: _toasts,
    _closeDialog,
    _dismissToast,

    // Public API
    confirm: (opts) => _openDialog({ ...opts, type: 'confirm' }),
    alert:   (opts) => _openDialog({ ...opts, type: 'alert' }),
    prompt:  (opts) => _openDialog({ ...opts, type: 'prompt' }),
    toast:   _addToast,
    // For loading toasts (ttl: 0) the caller needs to dismiss when the
    // operation finishes — exposed so callers don't have to reach into
    // the underscore-prefixed internals.
    dismissToast: _dismissToast,
  }
}
