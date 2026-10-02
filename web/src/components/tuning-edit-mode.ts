/** One owner for mobile tuning permissions, controls and media-query cleanup. */
import type { EqCurveCanvas } from './eq-curve-canvas';
import type { SurfaceScope } from '../ui/lifecycle';

export class TuningEditMode {
  enabled: boolean;
  private query = window.matchMedia('(max-width: 767px), (pointer: coarse)');

  constructor(
    private container: HTMLElement, scope: SurfaceScope,
    private editor: () => EqCurveCanvas | null,
    private state: () => { pointCount: number; undoAvailable: boolean },
  ) {
    this.enabled = !this.query.matches;
    const button = container.querySelector<HTMLButtonElement>('[data-tuning-edit-toggle]');
    if (button) scope.listen(button, 'click', () => { this.enabled = !this.enabled; this.update(); });
    scope.listen(this.query, 'change', () => {
      this.enabled = !this.query.matches;
      this.update();
    });
  }

  update() {
    const { undoAvailable } = this.state();
    this.editor()?.setReadOnly(!this.enabled);
    if (!this.enabled) this.container.querySelector<HTMLElement>('[data-eq-point-editor]')?.setAttribute('hidden', '');
    const mode = this.container.querySelector<HTMLElement>('[data-tuning-edit-mode]');
    if (mode) mode.hidden = !this.query.matches;
    const button = this.container.querySelector<HTMLButtonElement>('[data-tuning-edit-toggle]');
    if (button) {
      button.textContent = this.enabled ? '完成编辑' : '启用编辑';
      button.setAttribute('aria-pressed', String(this.enabled));
    }
    const hint = this.container.querySelector<HTMLElement>('[data-tuning-edit-hint]');
    if (hint) hint.textContent = this.enabled
      ? '编辑已启用，松手后自动应用。完成后关闭编辑，避免误触。'
      : '当前为查看模式，可滑动浏览；启用编辑后调整 EQ。';
    this.container.querySelectorAll<HTMLInputElement | HTMLButtonElement | HTMLSelectElement>(
      '[data-eq-field], [data-eq-add-point], [data-eq-remove], [data-tuning-curve], '
      + '[data-tuning-import], [data-tuning-night], [data-tuning-loudness], '
      + '[data-tuning-calibrate], [data-tuning-ab-toggle]',
    ).forEach(control => {
      control.disabled = !this.enabled;
    });
    const undo = this.container.querySelector<HTMLButtonElement>('[data-tuning-undo]');
    const destination = this.container.querySelector(this.query.matches ? '[data-tuning-undo-slot]' : '.tuning-toolbar');
    if (undo && destination && undo.parentElement !== destination) destination.prepend(undo);
    if (undo) undo.disabled = !this.enabled || !undoAvailable;
    this.container.querySelector<HTMLElement>('[data-point-delete]')?.setAttribute('hidden', '');
  }
}
