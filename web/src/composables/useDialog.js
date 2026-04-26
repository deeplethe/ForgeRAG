/**
 * Dialog + Toast system — replaces native confirm() / alert().
 *
 * Singleton reactive state shared app-wide. <DialogHost> (mounted once in
 * App.vue) renders both the modal dialog AND toast list. Any component
 * gets at the API via `useDialog()`.
 *
 * API:
 *
 *   const { confirm, alert, toast } = useDialog()
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
 *   // Toast — transient non-blocking notification.
 *   toast('Saved.')                                // info
 *   toast('Upload failed', { variant: 'error' })   // red
 *   toast('Done', { variant: 'success' })          // green
 */

import { reactive } from 'vue'

const _dialog = reactive({
  open: false,
  type: 'confirm',          // 'confirm' | 'alert'
  variant: 'default',       // 'default' | 'destructive'
  title: '',
  description: '',
  confirmText: 'Continue',
  cancelText: 'Cancel',
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
    _dialog._resolve = resolve
  })
}

function _closeDialog(value) {
  _dialog.open = false
  if (_dialog._resolve) {
    _dialog._resolve(value)
    _dialog._resolve = null
  }
}

function _addToast(message, opts = {}) {
  const id = ++_toastSeq
  const ttl = opts.ttl ?? 4000
  const item = {
    id,
    message,
    variant: opts.variant || 'info', // 'info' | 'success' | 'error' | 'warn'
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
    toast:   _addToast,
  }
}
