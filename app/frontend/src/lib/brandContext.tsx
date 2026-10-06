// React access to the brand, so components stop prop-drilling a name.
//
// Prop drilling was already failing: HubView.tsx ran its OWN /api/brand fetch
// because threading `brand` from App → HubView → panels was worse than a
// duplicate request. A context fixes the cause rather than the symptom, and it
// is what lets the ~30 components that hardcode "Ava" in placeholders and
// tooltips read the real name without every one of them growing a prop.
//
// Subscribes to BOTH `ava:brand` (same tab) and `storage` (other tabs) — the
// identical dual listener ThemeToggle.tsx already uses, which is the thing that
// makes a re-brand in one tab reach the others.

import type { ReactNode } from 'react';
import { createContext, useContext, useEffect, useState } from 'react';
import { type Brand, DEFAULT_BRAND, getBrand } from './brand';

const BrandContext = createContext<Brand>(DEFAULT_BRAND);

export function BrandProvider({ children }: { children: ReactNode }) {
  const [brand, setBrandState] = useState<Brand>(getBrand);

  useEffect(() => {
    const onBrand = (e: Event) => setBrandState((e as CustomEvent<Brand>).detail);
    const onStorage = (e: StorageEvent) => {
      if (e.key === 'ava.brand') setBrandState(getBrand());
    };
    window.addEventListener('ava:brand', onBrand);
    window.addEventListener('storage', onStorage);
    return () => {
      window.removeEventListener('ava:brand', onBrand);
      window.removeEventListener('storage', onStorage);
    };
  }, []);

  return <BrandContext.Provider value={brand}>{children}</BrandContext.Provider>;
}

/** The live brand. Defaults to Ava's, so a component outside the provider (a
 *  test, a storybook) still renders sensibly rather than crashing. */
export function useBrand(): Brand {
  return useContext(BrandContext);
}

/** Just the name — the overwhelmingly common case in copy. */
export function useBrandName(): string {
  return useContext(BrandContext).name || 'Ava';
}
