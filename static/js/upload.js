/**
 * Upload Zone Handler
 * 드래그앤드롭 및 클릭 기반 파일 업로드
 */
class UploadZone {
    constructor(zoneElement, inputElement, onFilesReady) {
        this.zone = zoneElement;
        this.input = inputElement;
        this.onFilesReady = onFilesReady;
        this.selectedFiles = [];

        this.init();
    }

    init() {
        this.setupDragDrop();
        this.setupClickUpload();
    }

    setupDragDrop() {
        ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
            this.zone.addEventListener(eventName, (e) => {
                e.preventDefault();
                e.stopPropagation();
            }, false);
        });

        ['dragenter', 'dragover'].forEach(eventName => {
            this.zone.addEventListener(eventName, () => {
                this.zone.classList.add('upload-zone-active');
            }, false);
        });

        ['dragleave', 'drop'].forEach(eventName => {
            this.zone.addEventListener(eventName, () => {
                this.zone.classList.remove('upload-zone-active');
            }, false);
        });

        this.zone.addEventListener('drop', (e) => {
            const files = [...e.dataTransfer.files];
            this.handleFiles(files);
        });
    }

    setupClickUpload() {
        // 존 클릭 시 파일 선택 다이얼로그 열기 (버튼 제외)
        this.zone.addEventListener('click', (e) => {
            // 버튼 또는 그 자식 요소를 클릭했으면 무시
            if (e.target.closest('button')) {
                return;
            }
            // 아이콘이나 텍스트 같은 다른 요소들은 처리
            if (e.target.tagName !== 'BUTTON') {
                this.input.click();
            }
        });

        // 파일 선택 input 이벤트
        this.input.addEventListener('change', (e) => {
            this.handleFiles([...e.target.files]);
        });
    }

    handleFiles(files) {
        // EML 파일만 필터링
        const emlFiles = files.filter(f => f.name.toLowerCase().endsWith('.eml'));

        if (emlFiles.length === 0) {
            this.showToast('EML 파일을 선택해주세요.', 'error');
            return;
        }

        this.selectedFiles = emlFiles;
        this.updateUI();

        // 콜백 실행
        if (this.onFilesReady) {
            this.onFilesReady(this.selectedFiles);
        }
    }

    updateUI() {
        // 파일 목록 표시 (선택된 파일 개수 표시)
        const count = this.selectedFiles.length;
        const fileNames = this.selectedFiles.map(f => f.name).join(', ');

        // 버튼 텍스트 업데이트
        const button = this.zone.querySelector('button');
        if (button) {
            button.textContent = count > 0 ? `${count}개 파일 선택됨 - 분석 시작` : '분석 시작';
        }

        // 파일 목록을 콘솔에도 출력
        console.log('Selected files:', this.selectedFiles);
    }

    clear() {
        this.selectedFiles = [];
        this.input.value = '';
        this.updateUI();
    }

    showToast(message, type = 'info') {
        const toast = document.createElement('div');
        toast.className = `toast fixed bottom-24 lg:bottom-8 right-4 px-4 py-3 rounded-lg text-sm font-medium z-50 ${
            type === 'error' ? 'bg-error text-on-error' :
            type === 'success' ? 'bg-green-600 text-white' :
            'bg-primary text-on-primary'
        }`;
        toast.textContent = message;
        document.body.appendChild(toast);

        setTimeout(() => {
            toast.classList.add('hide');
            setTimeout(() => toast.remove(), 300);
        }, 3000);
    }

    getFiles() {
        return this.selectedFiles;
    }
}

/**
 * Global upload zone initialization
 */
document.addEventListener('DOMContentLoaded', () => {
    const uploadZoneEl = document.getElementById('upload-zone');
    const fileInput = document.getElementById('file-input');

    if (uploadZoneEl && fileInput) {
        window.uploadZone = new UploadZone(uploadZoneEl, fileInput, (files) => {
            // 파일 선택 후 콜백 (app.js에서 처리)
            console.log('Files ready for upload:', files);
        });
    }
});
