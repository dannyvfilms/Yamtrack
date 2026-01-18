/**
 * Barcode Scanner Module for ISBN-13 decoding from images
 * Uses ZXing library to decode barcodes from uploaded photos
 */

// Barcode scanning functionality using ZXing
async function initBarcodeScanner() {
  // ZXing is loaded from CDN in base.html
  console.log('[BARCODE] initBarcodeScanner called, window.ZXing:', typeof window.ZXing, window.ZXing ? 'exists' : 'undefined');
  if (typeof window.ZXing === 'undefined') {
    console.error('[BARCODE] ZXing library not loaded - make sure the script tag loaded correctly');
    return null;
  }

  try {
    console.log('[BARCODE] Extracting ZXing components...');
    const { BrowserMultiFormatReader, BarcodeFormat, DecodeHintType } = window.ZXing;
    console.log('[BARCODE] ZXing components extracted:', {
      BrowserMultiFormatReader: !!BrowserMultiFormatReader,
      BarcodeFormat: !!BarcodeFormat,
      DecodeHintType: !!DecodeHintType,
      BrowserMultiFormatReaderType: typeof BrowserMultiFormatReader
    });
    
    const hints = new Map();
    const formats = [
      BarcodeFormat.EAN_13,
      BarcodeFormat.EAN_8,
      BarcodeFormat.UPC_A,
      BarcodeFormat.UPC_E,
    ];
    console.log('[BARCODE] Setting POSSIBLE_FORMATS:', formats);
    hints.set(DecodeHintType.POSSIBLE_FORMATS, formats);
    
    console.log('[BARCODE] Setting TRY_HARDER: true');
    hints.set(DecodeHintType.TRY_HARDER, true);
    
    console.log('[BARCODE] Setting CHARACTER_SET: UTF-8');
    hints.set(DecodeHintType.CHARACTER_SET, 'UTF-8');
    
    console.log('[BARCODE] Creating BrowserMultiFormatReader with hints...');
    const reader = new BrowserMultiFormatReader(hints);
    console.log('[BARCODE] Reader created, type:', typeof reader);
    console.log('[BARCODE] Reader methods available:', {
      decodeFromImageData: typeof reader.decodeFromImageData,
      decodeFromImageElement: typeof reader.decodeFromImageElement,
      decodeFromImage: typeof reader.decodeFromImage,
      decodeFromVideo: typeof reader.decodeFromVideo
    });
    
    return reader;
  } catch (error) {
    console.error('[BARCODE] Error initializing ZXing reader:', error);
    console.error('[BARCODE] Error stack:', error?.stack);
    return null;
  }
}

/**
 * Validate if a decoded code is a valid ISBN-13
 * ISBN-13 must be 13 digits and typically starts with 978 or 979
 */
function computeIsbn13CheckDigit(isbn12) {
  let sum = 0;
  for (let i = 0; i < isbn12.length; i += 1) {
    const digit = Number.parseInt(isbn12[i], 10);
    if (Number.isNaN(digit)) {
      return null;
    }
    sum += digit * (i % 2 === 0 ? 1 : 3);
  }
  return (10 - (sum % 10)) % 10;
}

function normalizeIsbn(code) {
  if (!code) return null;

  const cleaned = code.replace(/\D/g, '');

  if (cleaned.length === 10) {
    const isbn12 = `978${cleaned.slice(0, 9)}`;
    const checkDigit = computeIsbn13CheckDigit(isbn12);
    return checkDigit === null ? null : `${isbn12}${checkDigit}`;
  }

  if (cleaned.length > 13) {
    const candidate = cleaned.slice(0, 13);
    if (candidate.startsWith('978') || candidate.startsWith('979')) {
      return candidate;
    }
    return null;
  }

  if (cleaned.length === 13) {
    if (cleaned.startsWith('978') || cleaned.startsWith('979')) {
      return cleaned;
    }
    return null;
  }

  return null;
}

function loadImageFromUrl(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.decoding = 'async';
    img.onload = () => resolve(img);
    img.onerror = (error) => reject(error);
    img.src = url;
  });
}

function createCanvasFromImage(img, maxDimension) {
  const width = img.naturalWidth || img.width;
  const height = img.naturalHeight || img.height;
  const maxSide = Math.max(width, height);
  const scale = maxDimension ? Math.min(1, maxDimension / maxSide) : 1;
  const targetWidth = Math.max(1, Math.round(width * scale));
  const targetHeight = Math.max(1, Math.round(height * scale));
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  if (!ctx) {
    throw new Error('Canvas context not available');
  }

  canvas.width = targetWidth;
  canvas.height = targetHeight;
  ctx.drawImage(img, 0, 0, targetWidth, targetHeight);
  return canvas;
}

function cropCanvas(sourceCanvas, crop) {
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  if (!ctx) {
    throw new Error('Canvas context not available');
  }

  const sx = Math.max(0, Math.floor(crop.sx));
  const sy = Math.max(0, Math.floor(crop.sy));
  const sw = Math.max(1, Math.floor(crop.sw));
  const sh = Math.max(1, Math.floor(crop.sh));

  canvas.width = Math.min(sourceCanvas.width, sw);
  canvas.height = Math.min(sourceCanvas.height, sh);
  ctx.drawImage(sourceCanvas, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
  return canvas;
}

async function canvasToImage(canvas) {
  if (canvas.toBlob) {
    const blob = await new Promise((resolve, reject) => {
      canvas.toBlob((result) => {
        if (result) {
          resolve(result);
          return;
        }
        reject(new Error('Failed to create image blob'));
      }, 'image/jpeg', 0.9);
    });

    const url = URL.createObjectURL(blob);
    try {
      return await loadImageFromUrl(url);
    } finally {
      URL.revokeObjectURL(url);
    }
  }

  const dataUrl = canvas.toDataURL('image/jpeg', 0.9);
  return loadImageFromUrl(dataUrl);
}

/**
 * Decode barcode from image file
 * @param {File} file - Image file to decode
 * @returns {Promise<string|null>} Decoded ISBN-13 or null if failed
 */
async function decodeBarcodeFromFile(file) {
  console.log('[BARCODE] decodeBarcodeFromFile called with file:', file.name, file.type, file.size, 'bytes');
  try {
    console.log('[BARCODE] Initializing barcode scanner...');
    const reader = await initBarcodeScanner();
    if (!reader) {
      console.error('[BARCODE] Failed to initialize barcode reader - reader is null/undefined');
      return null;
    }
    console.log('[BARCODE] Reader initialized successfully, type:', typeof reader, 'has decodeFromImageData:', typeof reader.decodeFromImageData);

    console.log('[BARCODE] Creating blob URL from file...');
    const imageUrl = URL.createObjectURL(file);
    let originalImg;
    try {
      console.log('[BARCODE] Loading image from blob URL...');
      originalImg = await loadImageFromUrl(imageUrl);
      console.log('[BARCODE] Image loaded successfully');
    } finally {
      URL.revokeObjectURL(imageUrl);
      console.log('[BARCODE] Blob URL revoked');
    }

    const naturalWidth = originalImg.naturalWidth || originalImg.width;
    const naturalHeight = originalImg.naturalHeight || originalImg.height;
    console.log('[BARCODE] Original image dimensions:', naturalWidth, 'x', naturalHeight, '(natural:', originalImg.naturalWidth, 'x', originalImg.naturalHeight, ')');
    
    const maxDimension = 1600;
    console.log('[BARCODE] Creating base canvas with max dimension:', maxDimension);
    const baseCanvas = createCanvasFromImage(originalImg, maxDimension);
    console.log('[BARCODE] Base canvas created:', baseCanvas.width, 'x', baseCanvas.height);
    
    const candidates = [
      { label: 'full', canvas: baseCanvas },
    ];

    const height = baseCanvas.height;
    const width = baseCanvas.width;
    console.log('[BARCODE] Generating candidate crops. Base dimensions:', width, 'x', height);
    
    if (height >= 200) {
      const bottomHalfY = Math.floor(height * 0.5);
      const bottomHalfCanvas = cropCanvas(baseCanvas, { sx: 0, sy: bottomHalfY, sw: width, sh: height - bottomHalfY });
      candidates.push({
        label: 'bottom-half',
        canvas: bottomHalfCanvas,
      });
      console.log('[BARCODE] Added bottom-half candidate:', bottomHalfCanvas.width, 'x', bottomHalfCanvas.height);

      const bottomThirdY = Math.floor(height * 0.65);
      const bottomThirdCanvas = cropCanvas(baseCanvas, { sx: 0, sy: bottomThirdY, sw: width, sh: height - bottomThirdY });
      candidates.push({
        label: 'bottom-third',
        canvas: bottomThirdCanvas,
      });
      console.log('[BARCODE] Added bottom-third candidate:', bottomThirdCanvas.width, 'x', bottomThirdCanvas.height);

      const bandHeight = Math.max(1, Math.floor(height * 0.3));
      const bandY = Math.floor((height - bandHeight) / 2);
      const centerBandCanvas = cropCanvas(baseCanvas, { sx: 0, sy: bandY, sw: width, sh: bandHeight });
      candidates.push({
        label: 'center-band',
        canvas: centerBandCanvas,
      });
      console.log('[BARCODE] Added center-band candidate:', centerBandCanvas.width, 'x', centerBandCanvas.height);
    }

    console.log('[BARCODE] Starting decode attempts. Total candidates:', candidates.length);

    for (let i = 0; i < candidates.length; i++) {
      const candidate = candidates[i];
      try {
        console.log(`[BARCODE] [${i + 1}/${candidates.length}] Attempting decode for candidate: "${candidate.label}"`);
        console.log(`[BARCODE] Canvas dimensions:`, candidate.canvas.width, 'x', candidate.canvas.height);
        console.log('[BARCODE] Converting canvas to image element for ZXing...');
        const candidateImg = await canvasToImage(candidate.canvas);
        console.log('[BARCODE] Image element created:', {
          width: candidateImg.width,
          height: candidateImg.height,
          complete: candidateImg.complete,
          naturalWidth: candidateImg.naturalWidth,
          naturalHeight: candidateImg.naturalHeight
        });
        
        console.log('[BARCODE] Calling reader.decodeFromImageElement() with image element...');
        const decodeStartTime = performance.now();
        const result = await reader.decodeFromImageElement(candidateImg);
        const decodeTime = performance.now() - decodeStartTime;
        console.log(`[BARCODE] decodeFromImageElement completed in ${decodeTime.toFixed(2)}ms`);
        console.log('[BARCODE] Decode result:', result, 'type:', typeof result);
        
        if (!result) {
          console.log('[BARCODE] Result is null/undefined, trying next candidate...');
          continue;
        }

        console.log('[BARCODE] Result object keys:', result ? Object.keys(result) : 'none');
        const text = result.getText ? result.getText() : (result.text || result);
        console.log('[BARCODE] Extracted text from result:', text, 'type:', typeof text);
        
        const validatedISBN = normalizeIsbn(text);
        console.log('[BARCODE] Normalized ISBN:', validatedISBN);
        
        if (validatedISBN) {
          console.log('[BARCODE] SUCCESS! Valid ISBN found:', validatedISBN);
          return validatedISBN;
        } else {
          console.log('[BARCODE] Text did not normalize to valid ISBN, trying next candidate...');
        }
      } catch (error) {
        const errorName = error?.name || 'Error';
        const errorMessage = error?.message || '';
        const errorStack = error?.stack || '';
        console.log(`[BARCODE] Decode error for candidate "${candidate.label}":`, errorName, errorMessage);
        console.log('[BARCODE] Error details:', {
          name: errorName,
          message: errorMessage,
          constructor: error?.constructor?.name,
          stack: errorStack.substring(0, 500) // First 500 chars of stack
        });
        
        if (errorName !== 'NotFoundException' && errorName !== 'NoCodeFoundException') {
          console.error('[BARCODE] Unexpected error type (not NotFoundException/NoCodeFoundException):', error);
          console.error('[BARCODE] Full error object:', error);
        }
      }
    }

    console.log('[BARCODE] All decode attempts exhausted. No barcode found.');
    return null;
  } catch (error) {
    console.error('[BARCODE] Fatal error in decodeBarcodeFromFile:', error);
    console.error('[BARCODE] Error stack:', error?.stack);
    return null;
  }
}

/**
 * Alternative decode method using ZXing's image decoding
 * This is a fallback if the imageData method doesn't work
 */
async function decodeBarcodeFromImageElement(imgElement) {
  try {
    const reader = await initBarcodeScanner();
    if (!reader) return null;

    const result = await reader.decodeFromImageElement(imgElement);
    
    if (result) {
      return normalizeIsbn(result.getText());
    }
    
    return null;
  } catch (error) {
    console.error('Error decoding from image element:', error);
    return null;
  }
}

// Export for use in Alpine.js
window.barcodeScannerUtils = {
  decodeBarcodeFromFile,
  decodeBarcodeFromImageElement,
  normalizeIsbn,
};
